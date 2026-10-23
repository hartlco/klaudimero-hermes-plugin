"""Private SQLite state shared by the installed profiles, outside plugin code."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import time
import uuid
import base64

from .contracts import Problem


def data_directory():
    override = os.environ.get("KLAUDIMERO_DATA_DIR")
    if override:
        return Path(override).expanduser()
    from hermes_cli.profiles import get_profile_dir
    return get_profile_dir("default") / "plugin-data" / "klaudimero"


class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else data_directory()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise Problem(503, "unsafe_storage")
        self.path = self.directory / "companion.sqlite3"
        if self.path.is_symlink():
            raise Problem(503, "unsafe_storage")
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS avatars (owner TEXT, profile TEXT, revision INTEGER NOT NULL,
                    appearance TEXT NOT NULL, PRIMARY KEY(owner,profile));
                CREATE TABLE IF NOT EXISTS receipts (owner TEXT, request TEXT, digest TEXT NOT NULL,
                    result TEXT NOT NULL, PRIMARY KEY(owner,request));
                CREATE TABLE IF NOT EXISTS devices (owner TEXT, id TEXT, token TEXT NOT NULL,
                    environment TEXT NOT NULL, sessions TEXT NOT NULL, expires REAL NOT NULL,
                    PRIMARY KEY(owner,id));
                CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, owner TEXT, device TEXT,
                    payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, lease REAL NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS relay_revocations (grant_id TEXT PRIMARY KEY);
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('server_id',?)", (str(uuid.uuid4()),))
            if "encryption_key" not in {r[1] for r in db.execute("PRAGMA table_info(devices)")}:
                db.execute("ALTER TABLE devices ADD COLUMN encryption_key TEXT")
            if "relay_grant" not in {r[1] for r in db.execute("PRAGMA table_info(devices)")}:
                db.execute("ALTER TABLE devices ADD COLUMN relay_grant TEXT")
            if not db.execute("SELECT 1 FROM metadata WHERE key='encrypted_push_v2'").fetchone():
                # Never replay plaintext events left by the previous release.
                db.execute("UPDATE outbox SET payload='{}',next_attempt=1e30,lease=0")
                db.execute("INSERT INTO metadata VALUES ('encrypted_push_v2','1')")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @property
    def server_id(self):
        with self.connection() as db:
            return db.execute("SELECT value FROM metadata WHERE key='server_id'").fetchone()[0]

    def push_signing_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        with self.connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='push_signing_key'").fetchone()
            if row is None:
                candidate = Ed25519PrivateKey.generate().private_bytes_raw().hex()
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('push_signing_key',?)", (candidate,))
                row = db.execute("SELECT value FROM metadata WHERE key='push_signing_key'").fetchone()
            return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(row[0]))

    @property
    def push_signing_public_key(self):
        return base64.b64encode(self.push_signing_key().public_key().public_bytes_raw()).decode()

    def avatars(self, owner):
        with self.connection() as db:
            return {r["profile"]: {"revision": r["revision"], "appearance": json.loads(r["appearance"])}
                    for r in db.execute("SELECT * FROM avatars WHERE owner=?", (owner,))}

    def avatar_revision(self, owner):
        with self.connection() as db:
            return db.execute("SELECT coalesce(sum(revision),0) FROM avatars WHERE owner=?", (owner,)).fetchone()[0]

    def put_avatar(self, owner, profile, expected, request, value):
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(json.dumps([profile, expected, serialized]).encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            receipt = db.execute("SELECT * FROM receipts WHERE owner=? AND request=?", (owner, request)).fetchone()
            if receipt:
                if receipt["digest"] != digest:
                    raise Problem(409, "request_id_reused")
                return json.loads(receipt["result"])
            row = db.execute("SELECT revision FROM avatars WHERE owner=? AND profile=?", (owner, profile)).fetchone()
            revision = row[0] if row else 0
            if expected != revision:
                raise Problem(409, "avatar_revision_conflict")
            if not row and db.execute("SELECT count(*) FROM avatars WHERE owner=?", (owner,)).fetchone()[0] >= 128:
                raise Problem(422, "avatar_limit")
            result = {"revision": revision + 1, "appearance": value}
            db.execute("INSERT OR REPLACE INTO avatars VALUES (?,?,?,?)", (owner, profile, revision + 1, serialized))
            db.execute("INSERT INTO receipts VALUES (?,?,?,?)", (owner, request, digest, json.dumps(result)))
            return result

    def register_device(self, owner, device, token, environment, sessions, encryption_key=None, relay_grant=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT token,environment,encryption_key,relay_grant FROM devices WHERE owner=? AND id=?", (owner, device)).fetchone()
            if not existing and db.execute("SELECT count(*) FROM devices WHERE owner=?", (owner,)).fetchone()[0] >= 32:
                raise Problem(422, "device_limit")
            if existing and (existing["token"], existing["environment"], existing["encryption_key"], existing["relay_grant"]) != (token, environment, encryption_key, relay_grant):
                # Retire old-token events, including leased ones. Their stable IDs
                # stay as tombstones; a late failure cannot revoke the new token.
                db.execute("UPDATE outbox SET payload='{}',next_attempt=1e30,lease=0 WHERE owner=? AND device=?", (owner, device))
                if existing["relay_grant"] and existing["relay_grant"] != relay_grant:
                    db.execute("INSERT OR IGNORE INTO relay_revocations VALUES (?)", (existing["relay_grant"],))
            db.execute("INSERT OR REPLACE INTO devices (owner,id,token,environment,sessions,expires,encryption_key,relay_grant) VALUES (?,?,?,?,?,?,?,?)", (owner, device, token, environment,
                       json.dumps(sessions), time.time() + 30 * 86400, encryption_key, relay_grant))

    def remove_device(self, owner, device):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT relay_grant FROM devices WHERE owner=? AND id=?", (owner, device)).fetchone()
            if row and row[0]:
                db.execute("INSERT OR IGNORE INTO relay_revocations VALUES (?)", (row[0],))
            db.execute("DELETE FROM devices WHERE owner=? AND id=?", (owner, device))
            db.execute("DELETE FROM outbox WHERE owner=? AND device=?", (owner, device))

    def pending_revocations(self):
        with self.connection() as db:
            return [r[0] for r in db.execute("SELECT grant_id FROM relay_revocations LIMIT 8")]

    def finish_revocation(self, grant):
        with self.connection() as db:
            db.execute("DELETE FROM relay_revocations WHERE grant_id=?", (grant,))

    def has_managed_grant(self, owner):
        with self.connection() as db:
            return db.execute("SELECT 1 FROM devices WHERE owner=? AND relay_grant IS NOT NULL AND expires>? LIMIT 1", (owner, time.time())).fetchone() is not None

    def enqueue(self, profile, session, turn, kind):
        from .push_crypto import seal
        now = time.time()
        signing_key = self.push_signing_key()
        server_id = self.server_id
        with self.connection() as db:
            for device in db.execute("SELECT * FROM devices WHERE expires>? AND encryption_key IS NOT NULL", (now,)).fetchall():
                if {"profile_id": profile, "session_id": session} not in json.loads(device["sessions"]):
                    continue
                event = hashlib.sha256(json.dumps([device["owner"], device["id"], profile, session, turn, kind]).encode()).hexdigest()
                destination = {"protocol": 1, "event_id": event, "device_id": device["id"], "kind": kind, "server_id": server_id,
                           "profile_id": profile, "session_id": session}
                payload = {"event_id": event, "device_token": device["token"], "environment": device["environment"],
                           "encrypted": seal(device["encryption_key"], signing_key, destination, now=now)}
                if device["relay_grant"]:
                    payload = {"event_id": event, "grant_id": device["relay_grant"], "encrypted": payload["encrypted"]}
                db.execute("INSERT OR IGNORE INTO outbox (id,owner,device,payload) VALUES (?,?,?,?)",
                           (event, device["owner"], device["id"], json.dumps(payload)))

    def claim(self):
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM outbox WHERE NOT EXISTS (SELECT 1 FROM devices WHERE devices.owner=outbox.owner AND devices.id=outbox.device AND expires>?)", (now,))
            db.execute("UPDATE outbox SET payload='{}',next_attempt=1e30,lease=0 WHERE payload!='{}' AND json_type(payload,'$.encrypted') IS NULL")
            row = db.execute("SELECT * FROM outbox WHERE next_attempt<=? AND lease<=? ORDER BY rowid LIMIT 1", (now, now)).fetchone()
            if row:
                db.execute("UPDATE outbox SET lease=? WHERE id=?", (now + 60, row["id"]))
                return dict(row)

    def settle(self, event, *, retry=False, drop_device=False):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if drop_device:
                row = db.execute("SELECT owner,device,payload FROM outbox WHERE id=?", (event,)).fetchone()
                if row:
                    payload = json.loads(row["payload"])
                    if payload.get("grant_id"):
                        removed = db.execute("DELETE FROM devices WHERE owner=? AND id=? AND relay_grant=?", (row["owner"], row["device"], payload["grant_id"]))
                    else:
                        removed = db.execute("DELETE FROM devices WHERE owner=? AND id=? AND token=? AND environment=?",
                                         (row["owner"], row["device"], payload.get("device_token"), payload.get("environment")))
                    if removed.rowcount:
                        db.execute("DELETE FROM outbox WHERE owner=? AND device=?", (row["owner"], row["device"]))
                    else:
                        db.execute("UPDATE outbox SET payload='{}',next_attempt=1e30,lease=0 WHERE id=?", (event,))
            elif retry:
                db.execute("UPDATE outbox SET attempts=attempts+1, lease=0, next_attempt=? + min(3600,30 * (1 << min(attempts,7))) WHERE id=? AND payload!='{}'", (time.time(), event))
            else:
                # Keep the event id as a tombstone so a repeated hook cannot requeue it.
                db.execute("UPDATE outbox SET payload='{}',next_attempt=1e30,lease=0 WHERE id=?", (event,))

