"""Domain-separated relay authorization and encrypted APNs ownership proofs."""
import base64
import hashlib
import json
import time
import uuid

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RELAY_URL = "https://klaudimero-push.hartl.co"
REQUEST_DOMAIN = b"Klaudimero relay request v1\n"
CHALLENGE_DOMAIN = b"Klaudimero relay enrollment v1\x00"


def decode_key(value):
    if not isinstance(value, str) or len(value) != 44:
        raise ValueError("invalid_key")
    raw = base64.b64decode(value, validate=True)
    if len(raw) != 32 or raw == bytes(32) or base64.b64encode(raw).decode() != value:
        raise ValueError("invalid_key")
    return raw


def public_id(value):
    return hashlib.sha256(decode_key(value)).hexdigest()


def request_bytes(method, path, timestamp, request_id, body):
    return REQUEST_DOMAIN + f"{method.upper()}\n{path}\n{timestamp}\n{request_id}\n{hashlib.sha256(body).hexdigest()}".encode()


def sign_request(key, method, path, body=b"", *, now=None, request_id=None):
    timestamp = str(int(time.time() if now is None else now))
    identifier = request_id or str(uuid.uuid4())
    return {"X-Klaudimero-Key": base64.b64encode(key.public_key().public_bytes_raw()).decode(),
            "X-Klaudimero-Time": timestamp, "X-Klaudimero-Request": identifier,
            "X-Klaudimero-Signature": base64.b64encode(key.sign(request_bytes(method, path, timestamp, identifier, body))).decode()}


def verify_request(headers, method, path, body, *, now=None):
    key = headers["x-klaudimero-key"]
    timestamp, identifier = headers["x-klaudimero-time"], headers["x-klaudimero-request"]
    if not 1 <= len(timestamp) <= 11 or not timestamp.isdigit() or abs(int(timestamp) - (time.time() if now is None else now)) > 300:
        raise ValueError("expired_request")
    if str(uuid.UUID(identifier)) != identifier.lower():
        raise ValueError("invalid_request")
    signature = base64.b64decode(headers["x-klaudimero-signature"], validate=True)
    ed25519.Ed25519PublicKey.from_public_bytes(decode_key(key)).verify(signature, request_bytes(method, path, timestamp, identifier, body))
    return key


def seal_challenge(public_key, owner_key, challenge_id, proof, *, now=None):
    import os
    context = CHALLENGE_DOMAIN + challenge_id.encode() + b"\x00" + public_id(owner_key).encode()
    recipient = x25519.X25519PublicKey.from_public_bytes(decode_key(public_key))
    ephemeral = x25519.X25519PrivateKey.generate()
    salt, nonce = os.urandom(32), os.urandom(12)
    key = HKDF(hashes.SHA256(), 32, salt, context).derive(ephemeral.exchange(recipient))
    issued = int(time.time() if now is None else now)
    body = json.dumps({"proof": proof, "expires_at": issued + 300}, separators=(",", ":")).encode()
    return {"version": 1, "challenge_id": challenge_id, "owner_id": public_id(owner_key),
            "ephemeral_key": base64.b64encode(ephemeral.public_key().public_bytes_raw()).decode(),
            "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(AESGCM(key).encrypt(nonce, body, context)).decode()}
