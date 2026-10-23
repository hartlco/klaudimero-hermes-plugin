"""A small, authenticated, self-hostable relay. Generic alerts only."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Literal

import httpx
import jwt
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
import base64


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[2]
    key_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    ciphertext: str = Field(min_length=64, max_length=3200)

    @model_validator(mode="after")
    def valid_ciphertext(self):
        try:
            raw = base64.b64decode(self.ciphertext, validate=True)
            if not 48 <= len(raw) <= 2400:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError("invalid_envelope") from None
        return self


class Push(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    event_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    device_token: str = Field(min_length=32, max_length=512, pattern=r"^[0-9a-f]+$")
    environment: Literal["sandbox", "production"]
    encrypted: Envelope

    @model_validator(mode="after")
    def matching_event(self):
        if self.event_id != self.encrypted.event_id:
            raise ValueError("invalid_envelope")
        return self


class APNs:
    def __init__(self):
        self.key_path = os.environ.get("APNS_KEY_FILE", "")
        self.key_id = os.environ.get("APNS_KEY_ID", "")
        self.team_id = os.environ.get("APNS_TEAM_ID", "")
        self.topic = os.environ.get("APNS_TOPIC", "co.hartl.HermesApple")
        self.jwt = None
        self.issued_at = 0
        self.lock = threading.Lock()
        self.client = httpx.Client(http2=True, timeout=15, follow_redirects=False)

    @property
    def ready(self):
        return bool(self.key_id and self.team_id and self.key_path and Path(self.key_path).is_file())

    def token(self):
        with self.lock:
            if self.jwt is None or time.time() - self.issued_at > 40 * 60:
                self.issued_at = int(time.time())
                self.jwt = jwt.encode({"iss": self.team_id, "iat": self.issued_at}, Path(self.key_path).read_text(),
                                      algorithm="ES256", headers={"kid": self.key_id})
            return self.jwt

    def send(self, push):
        payload = {"aps": {"alert": {"title": "Klaudimero", "body": "Open Klaudimero to view an encrypted notification."},
                            "mutable-content": 1, "sound": "default"},
                   "klaudimero_encrypted": push.encrypted.model_dump()}
        return self.post(push.device_token, push.environment, push.event_id, payload)

    def send_challenge(self, token, environment, packet):
        return self.post(token, environment, packet["challenge_id"], {
            "aps": {"alert": {"title": "Klaudimero", "body": "Finishing notification setup."}},
            "klaudimero_relay_enrollment": packet})

    def post(self, device_token, environment, event_id, payload):
        host = "api.sandbox.push.apple.com" if environment == "sandbox" else "api.push.apple.com"
        return self.client.post(f"https://{host}/3/device/{device_token}", json=payload, headers={
            "authorization": "bearer " + self.token(), "apns-topic": self.topic, "apns-push-type": "alert",
            "apns-priority": "10", "apns-expiration": str(int(time.time()) + 3600),
            "apns-id": str(uuid.uuid5(uuid.NAMESPACE_URL, event_id)), "apns-collapse-id": event_id})


def create_app(provider=None, *, directory=None, clients=None):
    app = FastAPI(title="Klaudimero push relay", docs_url=None, redoc_url=None, openapi_url=None)
    from .body_limit import BodyLimit
    app.add_middleware(BodyLimit)
    provider = provider or APNs()
    clients = clients if clients is not None else json.loads(os.environ.get("KLAUDIMERO_RELAY_CLIENTS") or "{}")
    # Optional compatibility for existing private installations. Managed grants
    # authorize each device/host pair without a shared operator password.
    if not isinstance(clients, dict) or any(not isinstance(k, str) or not isinstance(v, str) or len(v) < 32 for k, v in clients.items()):
        raise ValueError("Relay clients must map installation names to credentials of at least 32 characters")
    root = Path(directory or os.environ.get("KLAUDIMERO_RELAY_DATA", "/data"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "delivery.sqlite3"
    if path.is_symlink():
        raise ValueError("Unsafe relay data path")

    @contextmanager
    def database():
        with sqlite3.connect(path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            yield db

    with database() as db:
        db.execute("CREATE TABLE IF NOT EXISTS deliveries (host TEXT,event TEXT,digest TEXT,state TEXT,lease REAL,created REAL,PRIMARY KEY(host,event))")
        db.execute("CREATE TABLE IF NOT EXISTS limits (host TEXT,window INTEGER,count INTEGER,PRIMARY KEY(host,window))")
    os.chmod(path, 0o600)

    def authenticate(authorization: str = Header(default="")):
        supplied = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        for host, credential in clients.items():
            if hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(credential.encode()).digest()):
                return host
        raise HTTPException(401, detail="Unauthorized")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "invalid_request"}, status_code=422)

    @app.get("/v1/status")
    def status(host=Depends(authenticate)):
        return {"protocol": 1, "apns_ready": provider.ready}

    def deliver(push: Push, host, *, digest_override=None):
        if not provider.ready:
            raise HTTPException(503, detail="apns_not_configured")
        serialized = push.model_dump_json()
        digest = digest_override or hashlib.sha256(serialized.encode()).hexdigest()
        now = time.time()
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest,state,lease FROM deliveries WHERE host=? AND event=?", (host, push.event_id)).fetchone()
            if row and row[0] != digest:
                raise HTTPException(409, detail="event_id_reused")
            if row and row[1] == "sent":
                return {"accepted": True, "duplicate": True}
            if row and row[2] > now:
                raise HTTPException(503, detail="delivery_pending", headers={"Retry-After": "30"})
            window = int(now // 60)
            db.execute("DELETE FROM limits WHERE window<?", (window - 2,))
            db.execute("INSERT INTO limits VALUES (?,?,1) ON CONFLICT(host,window) DO UPDATE SET count=count+1", (host, window))
            if db.execute("SELECT count FROM limits WHERE host=? AND window=?", (host, window)).fetchone()[0] > 120:
                raise HTTPException(429, detail="rate_limited", headers={"Retry-After": "60"})
            db.execute("INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?,?,?)", (host, push.event_id, digest, "sending", now + 60, now))
        try:
            response = provider.send(push)
        except Exception:
            # Preserve the delivery lease after an ambiguous response. Retrying uses
            # the same APNs id/collapse id; do not claim exactly-once device delivery.
            raise HTTPException(503, detail="delivery_uncertain") from None
        if response.status_code == 200:
            with database() as db:
                db.execute("UPDATE deliveries SET state='sent',lease=0 WHERE host=? AND event=?", (host, push.event_id))
            return {"accepted": True}
        try:
            reason = response.json().get("reason", "")
        except (ValueError, AttributeError):
            reason = ""
        if response.status_code == 410 or reason in {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"}:
            raise HTTPException(410, detail="invalid_device")
        with database() as db:
            db.execute("UPDATE deliveries SET lease=0 WHERE host=? AND event=?", (host, push.event_id))
        raise HTTPException(503, detail="apns_rejected")

    @app.post("/v1/push")
    def legacy_delivery(push: Push, host=Depends(authenticate)):
        return deliver(push, host)

    from .managed import install
    install(app, provider, database, deliver, Envelope)
    return app

