import base64
import hashlib
import json
import sqlite3
import time
import uuid

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from klaudimero_plugin.relay_auth import CHALLENGE_DOMAIN, public_id, sign_request
from relay.app import create_app


def public(key):
    return base64.b64encode(key.public_key().public_bytes_raw()).decode()


def signed(client, key, method, path, value=None, *, sign_path=None, now=None):
    raw = json.dumps(value, separators=(",", ":")).encode() if value is not None else b""
    return client.request(method, path, content=raw, headers={**sign_request(key, method, sign_path or path, raw, now=now), "Content-Type": "application/json"})


class Provider:
    ready = True
    def __init__(self):
        self.challenges, self.calls = [], []
    def send_challenge(self, token, environment, packet):
        self.challenges.append((token, environment, packet))
        return type("Response", (), {"status_code": 200})()
    def send(self, push):
        self.calls.append(push)
        return type("Response", (), {"status_code": 200})()


@pytest.fixture
def relay(tmp_path):
    provider = Provider()
    with TestClient(create_app(provider, directory=tmp_path, clients={})) as client:
        yield client, provider, tmp_path


def challenge(client, provider, owner, token="a" * 64):
    encryption = x25519.X25519PrivateKey.generate()
    body = {"challenge_id": str(uuid.uuid4()), "device_token": token, "environment": "production", "encryption_key": public(encryption)}
    response = signed(client, owner, "POST", "/v2/devices/challenges", body)
    assert response.status_code == 200, response.text
    assert not response.json()["verified"]
    assert set(response.json()) == {"challenge_id", "verified"}
    # Test-only APNs capture: a caller of the HTTP enrollment API cannot access
    # this packet. The actual app must receive it on the addressed Apple device.
    packet = provider.challenges[-1][2]
    context = CHALLENGE_DOMAIN + body["challenge_id"].encode() + b"\x00" + public_id(public(owner)).encode()
    key = HKDF(hashes.SHA256(), 32, base64.b64decode(packet["salt"]), context).derive(encryption.exchange(x25519.X25519PublicKey.from_public_bytes(base64.b64decode(packet["ephemeral_key"]))))
    clear = AESGCM(key).decrypt(base64.b64decode(packet["nonce"]), base64.b64decode(packet["ciphertext"]), context)
    proof = json.loads(clear)["proof"]
    assert proof not in response.text and proof not in json.dumps(packet)
    return body, proof


def enroll(client, provider, owner, token="a" * 64):
    body, proof = challenge(client, provider, owner, token)
    result = signed(client, owner, "POST", "/v2/devices/challenges/" + body["challenge_id"] + "/confirm", {"proof": proof})
    assert result.status_code == 200, result.text
    return body


def grant(client, owner, host):
    recipient = x25519.X25519PrivateKey.generate()
    body = {"grant_id": str(uuid.uuid4()), "host_key": public(host), "recipient_key_id": hashlib.sha256(recipient.public_key().public_bytes_raw()).hexdigest()}
    response = signed(client, owner, "POST", "/v2/grants", body)
    assert response.status_code == 200, response.text
    return body


def payload(grant):
    return {"grant_id": grant["grant_id"], "event_id": "b" * 64,
            "encrypted": {"version": 2, "key_id": grant["recipient_key_id"], "event_id": "b" * 64,
                          "ciphertext": base64.b64encode(b"synthetic encrypted data" * 8).decode()}}


def test_no_env_or_shared_password_and_only_device_proof_can_authorize_host(relay):
    client, provider, _ = relay
    owner, host, attacker = [ed25519.Ed25519PrivateKey.generate() for _ in range(3)]
    body = {"grant_id": str(uuid.uuid4()), "host_key": public(host), "recipient_key_id": "c" * 64}
    assert signed(client, owner, "POST", "/v2/grants", body).status_code == 403
    challenge_body, proof = challenge(client, provider, owner)
    path = "/v2/devices/challenges/" + challenge_body["challenge_id"] + "/confirm"
    assert signed(client, owner, "POST", path, {"proof": "0" * 64}).status_code == 403
    assert signed(client, attacker, "POST", path, {"proof": proof}).status_code == 403
    assert signed(client, owner, "POST", path, {"proof": proof}).status_code == 200
    scope = grant(client, owner, host)
    view = signed(client, host, "GET", "/v2/grants/" + scope["grant_id"])
    assert view.status_code == 200
    assert set(view.json()) == {"grant_id", "host_key", "recipient_key_id", "expires_at"}
    assert signed(client, attacker, "GET", "/v2/grants/" + scope["grant_id"]).status_code == 410
    assert signed(client, attacker, "POST", "/v2/push", payload(scope)).status_code == 410
    assert signed(client, host, "POST", "/v2/push", payload(scope)).json()["accepted"]
    assert provider.calls[0].device_token == "a" * 64


def test_revocation_expiry_recipient_scoping_and_no_replay_resurrection(relay):
    client, provider, directory = relay
    owner, host, other = [ed25519.Ed25519PrivateKey.generate() for _ in range(3)]
    enroll(client, provider, owner)
    scope = grant(client, owner, host)
    changed = payload(scope); changed["encrypted"]["key_id"] = "f" * 64
    assert signed(client, host, "POST", "/v2/push", changed).status_code == 403
    assert signed(client, other, "DELETE", "/v2/grants/" + scope["grant_id"]).status_code == 404
    assert signed(client, owner, "DELETE", "/v2/grants/" + scope["grant_id"]).status_code == 200
    assert signed(client, host, "POST", "/v2/push", payload(scope)).status_code == 410
    assert signed(client, owner, "POST", "/v2/grants", scope).status_code == 409
    scope2 = grant(client, owner, host)
    with sqlite3.connect(directory / "delivery.sqlite3") as db:
        db.execute("UPDATE managed_grants SET expires=? WHERE id=?", (time.time() - 1, scope2["grant_id"]))
    assert signed(client, host, "POST", "/v2/push", payload(scope2)).status_code == 410


def test_token_rotation_requires_delivery_and_preserves_event_deduplication(relay):
    client, provider, _ = relay
    owner, host = ed25519.Ed25519PrivateKey.generate(), ed25519.Ed25519PrivateKey.generate()
    enroll(client, provider, owner)
    scope = grant(client, owner, host)
    assert signed(client, host, "POST", "/v2/push", payload(scope)).status_code == 200
    body, proof = challenge(client, provider, owner, token="d" * 64)
    assert signed(client, owner, "POST", "/v2/devices/challenges/" + body["challenge_id"] + "/confirm", {"proof": proof}).status_code == 200
    assert signed(client, host, "POST", "/v2/push", payload(scope)).json()["duplicate"]
    assert len(provider.calls) == 1
    another = payload(scope); another["event_id"] = another["encrypted"]["event_id"] = "e" * 64
    assert signed(client, host, "POST", "/v2/push", another).status_code == 200
    assert provider.calls[-1].device_token == "d" * 64


def test_signatures_body_limits_plaintext_rejection_and_challenge_throttling(relay):
    client, provider, _ = relay
    owner, host = ed25519.Ed25519PrivateKey.generate(), ed25519.Ed25519PrivateKey.generate()
    assert client.post("/v2/grants", json={}).status_code == 401
    assert signed(client, owner, "POST", "/v2/grants", {}, now=time.time() - 301).status_code == 401
    assert signed(client, owner, "POST", "/v2/grants", {}, sign_path="/other").status_code == 401
    assert client.post("/v2/grants", content=b"x" * 8193).status_code == 413
    enroll(client, provider, owner)
    scope = grant(client, owner, host)
    assert signed(client, host, "POST", "/v2/push", {**payload(scope), "device_token": "a" * 64}).status_code == 422
    assert signed(client, host, "POST", "/v2/push", {**payload(scope), "title": "not permitted"}).status_code == 422
    for _ in range(4):
        challenge(client, provider, ed25519.Ed25519PrivateKey.generate())
    body = {"challenge_id": str(uuid.uuid4()), "device_token": "a" * 64, "environment": "production", "encryption_key": public(x25519.X25519PrivateKey.generate())}
    assert signed(client, owner, "POST", "/v2/devices/challenges", body).json()["verified"]
    assert signed(client, ed25519.Ed25519PrivateKey.generate(), "POST", "/v2/devices/challenges", body).status_code == 429


def test_registered_device_renews_without_another_push_and_host_grants_survive_restart(relay):
    client, provider, directory = relay
    owner, host = ed25519.Ed25519PrivateKey.generate(), ed25519.Ed25519PrivateKey.generate()
    body = enroll(client, provider, owner)
    body["challenge_id"] = str(uuid.uuid4())
    assert signed(client, owner, "POST", "/v2/devices/challenges", body).json()["verified"]
    assert len(provider.challenges) == 1
    scope = grant(client, owner, host)
    with TestClient(create_app(provider, directory=directory, clients={})) as restarted:
        assert signed(restarted, host, "GET", "/v2/grants/" + scope["grant_id"]).status_code == 200
        assert signed(restarted, host, "DELETE", "/v2/grants/" + scope["grant_id"]).status_code == 200


def test_uncertain_challenge_does_not_redispatch_and_proof_cannot_rotate_back(relay):
    client, provider, _ = relay
    owner = ed25519.Ed25519PrivateKey.generate()
    old, proof = challenge(client, provider, owner)
    assert signed(client, owner, "POST", "/v2/devices/challenges", old).status_code == 200
    assert len(provider.challenges) == 1
    fresh, fresh_proof = challenge(client, provider, owner, token="f" * 64)
    assert signed(client, owner, "POST", "/v2/devices/challenges/" + fresh["challenge_id"] + "/confirm", {"proof": fresh_proof}).status_code == 200
    assert signed(client, owner, "POST", "/v2/devices/challenges/" + old["challenge_id"] + "/confirm", {"proof": proof}).status_code == 403
    provider.send_challenge = lambda *args: (_ for _ in ()).throw(TimeoutError())
    uncertain = {**old, "challenge_id": str(uuid.uuid4()), "device_token": "e" * 64}
    assert signed(client, owner, "POST", "/v2/devices/challenges", uncertain).status_code == 503
    provider.send_challenge = lambda *args: pytest.fail("An uncertain dispatch was replayed")
    assert signed(client, owner, "POST", "/v2/devices/challenges", uncertain).status_code == 200
