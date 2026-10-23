"""Device-authorized grants. A Hermes host never receives an APNs token."""
import hashlib
import hmac
import json
import secrets
import time
import uuid

from cryptography.exceptions import InvalidSignature
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from klaudimero_plugin.relay_auth import decode_key, public_id, seal_challenge, verify_request


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Challenge(Body):
    challenge_id: str = Field(min_length=36, max_length=36)
    device_token: str = Field(min_length=32, max_length=512, pattern=r"^[0-9a-f]+$")
    environment: Literal["production", "sandbox"]
    encryption_key: str = Field(min_length=44, max_length=44)


class Proof(Body):
    proof: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")


class Grant(Body):
    grant_id: str = Field(min_length=36, max_length=36)
    host_key: str = Field(min_length=44, max_length=44)
    recipient_key_id: str = Field(pattern=r"^[0-9a-f]{64}$")


def identifier(value):
    try:
        parsed = str(uuid.UUID(value))
        if parsed != value.lower():
            raise ValueError()
        return parsed
    except (ValueError, AttributeError):
        raise HTTPException(422, detail="invalid_identifier") from None


def install(app, provider, database, deliver, Envelope):
    class ManagedPush(Body):
        grant_id: str = Field(min_length=36, max_length=36)
        event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
        encrypted: Envelope

    with database() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS managed_devices (
                owner TEXT PRIMARY KEY, token TEXT NOT NULL, environment TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS managed_challenges (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, token TEXT NOT NULL, environment TEXT NOT NULL,
                recipient TEXT NOT NULL, proof_hash TEXT NOT NULL, expires REAL NOT NULL,
                packet TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS managed_grants (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, host TEXT NOT NULL,
                recipient TEXT NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS managed_limits (bucket TEXT, window INTEGER, count INTEGER,
                PRIMARY KEY(bucket,window));
        """)

    async def actor(request: Request):
        try:
            return verify_request(request.headers, request.method, request.url.path, await request.body())
        except (KeyError, ValueError, TypeError, InvalidSignature):
            raise HTTPException(401, detail="invalid_relay_signature") from None

    def limit(db, bucket, maximum, duration):
        now = time.time()
        db.execute("DELETE FROM managed_limits WHERE (bucket='challenge-global' OR bucket LIKE 'challenge-peer:%') AND window<?", (int(now // 60) - 1,))
        db.execute("DELETE FROM managed_limits WHERE bucket LIKE 'challenge-device:%' AND window<?", (int(now // 300) - 1,))
        db.execute("DELETE FROM managed_limits WHERE bucket LIKE 'grant-owner:%' AND window<?", (int(now // 86400) - 1,))
        window = int(now // duration)
        db.execute("DELETE FROM managed_limits WHERE window < ? AND bucket=?", (window - 1, bucket))
        db.execute("INSERT INTO managed_limits VALUES (?,?,1) ON CONFLICT(bucket,window) DO UPDATE SET count=count+1", (bucket, window))
        if db.execute("SELECT count FROM managed_limits WHERE bucket=? AND window=?", (bucket, window)).fetchone()[0] > maximum:
            raise HTTPException(429, detail="registration_rate_limited", headers={"Retry-After": "60"})

    def view(row):
        return {"grant_id": row["id"], "host_key": row["host"], "recipient_key_id": row["recipient"], "expires_at": int(row["expires"])}

    def active_grant(db, grant_id, host):
        row = db.execute("SELECT * FROM managed_grants WHERE id=? AND host=?", (identifier(grant_id), host)).fetchone()
        if not row or row["revoked"] or row["expires"] <= time.time():
            raise HTTPException(410, detail="grant_inactive")
        device = db.execute("SELECT * FROM managed_devices WHERE owner=? AND expires>?", (row["owner"], time.time())).fetchone()
        if not device:
            raise HTTPException(410, detail="device_inactive")
        return row, device

    @app.post("/v2/devices/challenges")
    def challenge(body: Challenge, request: Request, owner=Depends(actor)):
        challenge_id = identifier(body.challenge_id)
        try:
            decode_key(body.encryption_key)
        except (ValueError, TypeError):
            raise HTTPException(422, detail="invalid_encryption_key") from None
        if len(body.device_token) % 2:
            raise HTTPException(422, detail="invalid_device_token")
        now = time.time()
        with database() as db:
            current = db.execute("SELECT * FROM managed_devices WHERE owner=? AND expires>?", (owner, now)).fetchone()
            if current and (current["token"], current["environment"]) == (body.device_token, body.environment):
                db.execute("UPDATE managed_devices SET expires=? WHERE owner=?", (now + 45 * 86400, owner))
                return {"challenge_id": challenge_id, "verified": True}
        if not provider.ready:
            raise HTTPException(503, detail="apns_not_configured")
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM managed_challenges WHERE expires<?", (now,))
            row = db.execute("SELECT * FROM managed_challenges WHERE id=?", (challenge_id,)).fetchone()
            if row:
                if (row["owner"], row["token"], row["environment"], row["recipient"]) != (owner, body.device_token, body.environment, body.encryption_key):
                    raise HTTPException(409, detail="challenge_id_reused")
                if row["sent"]:
                    return {"challenge_id": challenge_id, "verified": False}
                packet = json.loads(row["packet"])
            else:
                peer = request.client.host if request.client else "unknown"
                limit(db, "challenge-global", 100, 60)
                limit(db, "challenge-peer:" + hashlib.sha256(peer.encode()).hexdigest(), 20, 60)
                limit(db, "challenge-device:" + hashlib.sha256((body.device_token + body.environment).encode()).hexdigest(), 5, 300)
                if db.execute("SELECT count(*) FROM managed_challenges").fetchone()[0] >= 3000:
                    raise HTTPException(503, detail="registration_busy")
                proof = secrets.token_hex(32)
                try:
                    packet = seal_challenge(body.encryption_key, owner, challenge_id, proof)
                except ValueError:
                    raise HTTPException(422, detail="invalid_encryption_key") from None
                db.execute("INSERT INTO managed_challenges (id,owner,token,environment,recipient,proof_hash,expires,packet) VALUES (?,?,?,?,?,?,?,?)",
                           (challenge_id, owner, body.device_token, body.environment, body.encryption_key,
                            hashlib.sha256(proof.encode()).hexdigest(), now + 300, json.dumps(packet)))
            # Claim before network I/O. A timed-out APNs response must not let
            # replays of the same challenge bypass delivery rate limits.
            db.execute("UPDATE managed_challenges SET sent=1 WHERE id=?", (challenge_id,))
        try:
            response = provider.send_challenge(body.device_token, body.environment, packet)
        except Exception:
            raise HTTPException(503, detail="verification_delivery_uncertain") from None
        if response.status_code != 200:
            raise HTTPException(503, detail="verification_delivery_failed")
        return {"challenge_id": challenge_id, "verified": False}

    @app.post("/v2/devices/challenges/{challenge_id}/confirm")
    def confirm(challenge_id: str, body: Proof, owner=Depends(actor)):
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM managed_challenges WHERE id=? AND owner=? AND expires>?", (identifier(challenge_id), owner, time.time())).fetchone()
            if not row or not hmac.compare_digest(row["proof_hash"], hashlib.sha256(body.proof.encode()).hexdigest()):
                raise HTTPException(403, detail="device_verification_failed")
            # Knowing a token or minting a signing key is insufficient: the proof
            # exists only in the encrypted APNs packet delivered to that token.
            db.execute("INSERT OR REPLACE INTO managed_devices VALUES (?,?,?,?)", (owner, row["token"], row["environment"], time.time() + 45 * 86400))
            db.execute("DELETE FROM managed_challenges WHERE owner=?", (owner,))
        return {"verified": True}

    @app.post("/v2/grants")
    def grant(body: Grant, owner=Depends(actor)):
        grant_id = identifier(body.grant_id)
        try:
            decode_key(body.host_key)
        except (ValueError, TypeError):
            raise HTTPException(422, detail="invalid_host_key") from None
        now = time.time()
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM managed_grants WHERE expires<? AND revoked=0", (now - 86400,))
            db.execute("DELETE FROM managed_devices WHERE expires<? AND NOT EXISTS (SELECT 1 FROM managed_grants WHERE managed_grants.owner=managed_devices.owner AND revoked=0 AND expires>?)", (now, now))
            if not db.execute("SELECT 1 FROM managed_devices WHERE owner=? AND expires>?", (owner, now)).fetchone():
                raise HTTPException(403, detail="device_verification_required")
            row = db.execute("SELECT * FROM managed_grants WHERE id=?", (grant_id,)).fetchone()
            if row and (row["owner"], row["host"], row["recipient"], row["revoked"]) != (owner, body.host_key, body.recipient_key_id, 0):
                raise HTTPException(409, detail="grant_id_reused")
            if not row:
                if db.execute("SELECT count(*) FROM managed_grants WHERE owner=? AND expires>? AND revoked=0", (owner, now)).fetchone()[0] >= 32:
                    raise HTTPException(422, detail="grant_limit")
                limit(db, "grant-owner:" + public_id(owner), 100, 86400)
            db.execute("INSERT INTO managed_grants VALUES (?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET expires=excluded.expires", (grant_id, owner, body.host_key, body.recipient_key_id, now + 30 * 86400))
            return view(db.execute("SELECT * FROM managed_grants WHERE id=?", (grant_id,)).fetchone())

    @app.get("/v2/grants/{grant_id}")
    def get_grant(grant_id: str, host=Depends(actor)):
        with database() as db:
            row, _ = active_grant(db, grant_id, host)
            return view(row)

    @app.delete("/v2/grants/{grant_id}")
    def revoke(grant_id: str, actor_key=Depends(actor)):
        with database() as db:
            row = db.execute("SELECT owner,host FROM managed_grants WHERE id=?", (identifier(grant_id),)).fetchone()
            if row and actor_key not in (row["owner"], row["host"]):
                raise HTTPException(404, detail="grant_not_found")
            if row:
                db.execute("UPDATE managed_grants SET revoked=1 WHERE id=?", (identifier(grant_id),))
        return {"revoked": True}

    @app.post("/v2/push")
    def push(body: ManagedPush, host=Depends(actor)):
        if body.encrypted.event_id != body.event_id:
            raise HTTPException(422, detail="invalid_envelope")
        with database() as db:
            grant, device = active_grant(db, body.grant_id, host)
            if body.encrypted.key_id != grant["recipient"]:
                raise HTTPException(403, detail="recipient_not_authorized")
        from .app import Push
        # Only the service can resolve a grant to a device token. The host cannot
        # supply another token, environment, recipient or unsigned event body.
        return deliver(Push(event_id=body.event_id, device_token=device["token"], environment=device["environment"], encrypted=body.encrypted),
                       "grant:" + grant["id"], digest_override=hashlib.sha256(body.model_dump_json().encode()).hexdigest())
