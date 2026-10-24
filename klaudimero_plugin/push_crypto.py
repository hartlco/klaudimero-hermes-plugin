"""RFC 9180 HPKE plus an authenticated Hermes signing identity. No relay keys."""
import base64
import hashlib
import json
import time

from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.hpke import AEAD, KDF, KEM, Suite

from .contracts import Problem

ALGORITHM = "hpke-x25519-sha256-aes128gcm-ed25519-v2"
DOMAIN = b"Klaudimero encrypted push v2\x00"
SUITE = Suite(KEM.X25519, KDF.HKDF_SHA256, AEAD.AES_128_GCM)


def decode_public_key(value):
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) != 32 or raw == bytes(32):
            raise ValueError()
        key = x25519.X25519PublicKey.from_public_bytes(raw)
        # Reject low-order points as well as incorrectly sized encodings.
        x25519.X25519PrivateKey.generate().exchange(key)
        return key
    except (ValueError, TypeError):
        raise Problem(422, "invalid_push_public_key") from None


def key_id(public_key):
    return hashlib.sha256(public_key.public_bytes_raw()).hexdigest()


def context(recipient_id, event_id):
    return DOMAIN + recipient_id.encode("ascii") + b"\x00" + event_id.encode("ascii")


def seal(public_key, signing_key, destination, now=None, notification=None):
    recipient = decode_public_key(public_key)
    recipient_id = key_id(recipient)
    info = context(recipient_id, destination["event_id"])
    issued = int(time.time() if now is None else now)
    content = {"destination": destination, "issued_at": issued, "expires_at": issued + 3600}
    if notification is not None:
        if (not isinstance(notification, dict) or set(notification) != {"title", "body"}
                or any(not isinstance(notification[k], str) or not notification[k].strip() for k in notification)
                or len(notification["title"].encode()) > 96 or len(notification["body"].encode()) > 512):
            raise Problem(422, "invalid_notification_preview")
        content["notification"] = dict(notification)
    while True:
        payload = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        signature = signing_key.sign(info + payload)
        signed = json.dumps({"payload": base64.b64encode(payload).decode(), "signature": base64.b64encode(signature).decode()},
                            separators=(",", ":")).encode()
        if len(signed) + 48 <= 2400:
            break
        if "notification" not in content:
            raise Problem(422, "push_payload_too_large")
        body = content["notification"]["body"]
        if len(body.encode()) <= 32:
            content.pop("notification")
        else:
            from .notification_preview import bounded
            content["notification"]["body"] = bounded(body, max(32, len(body.encode()) * 3 // 4))
    # A new HPKE context/ephemeral key for every event. Persist the sealed packet
    # before delivery; retries reuse exactly those bytes, never reseal an event.
    encrypted = SUITE.encrypt(signed, recipient, info=info)
    if len(encrypted) > 2400:
        raise Problem(422, "push_payload_too_large")
    return {"version": 2, "key_id": recipient_id, "event_id": destination["event_id"],
            "ciphertext": base64.b64encode(encrypted).decode()}
