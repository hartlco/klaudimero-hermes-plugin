import base64
import json
import uuid
import pytest
from fastapi.testclient import TestClient
from relay.app import APNs, create_app


class Response:
    def __init__(self, status=200, reason=""):
        self.status_code, self.reason = status, reason
    def json(self):
        return {"reason": self.reason}


class Provider:
    ready = True
    def __init__(self): self.calls = []
    def send(self, push): self.calls.append(push); return Response()


@pytest.fixture
def payload():
    return {"event_id": "a" * 64, "device_token": "b" * 64, "environment": "production",
            "encrypted": {"version": 2, "key_id": "c" * 64, "event_id": "a" * 64,
                          "ciphertext": base64.b64encode(b"synthetic ciphertext" * 10).decode()}}


def test_relay_authenticated_idempotent_and_has_no_content_api(tmp_path, payload):
    provider = Provider()
    app = create_app(provider, directory=tmp_path, clients={"host": "k" * 32})
    with TestClient(app) as client:
        assert client.post("/v1/push", json=payload).status_code == 401
        client.headers["Authorization"] = "Bearer " + "k" * 32
        assert client.post("/v1/push", json=payload).status_code == 200
        assert client.post("/v1/push", json=payload).json()["duplicate"]
        assert len(provider.calls) == 1
        assert client.post("/v1/push", json={**payload, "title": "Arbitrary spam is forbidden"}).status_code == 422
        assert client.post("/v1/push", json={**payload, "encrypted": {**payload["encrypted"], "ciphertext": base64.b64encode(b"different bytes" * 10).decode()}}).status_code == 409


def test_missing_apple_key_is_an_honest_not_ready_state(tmp_path, payload):
    provider = Provider(); provider.ready = False
    with TestClient(create_app(provider, directory=tmp_path, clients={"host": "k" * 32})) as client:
        client.headers["Authorization"] = "Bearer " + "k" * 32
        assert not client.get("/v1/status").json()["apns_ready"]
        assert client.post("/v1/push", json=payload).json()["detail"] == "apns_not_configured"


def test_apns_packet_is_generic_and_correctly_signed(tmp_path, monkeypatch, payload):
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    import jwt
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "synthetic.p8"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    for name, value in {"APNS_KEY_FILE": str(path), "APNS_KEY_ID": "KEY1234567", "APNS_TEAM_ID": "TEAM123456"}.items(): monkeypatch.setenv(name, value)
    provider = APNs()
    class Client:
        def post(self, url, **kwargs): self.url, self.kwargs = url, kwargs; return Response()
    provider.client = Client()
    from relay.app import Push
    provider.send(Push(**payload))
    assert provider.client.url == "https://api.push.apple.com/3/device/" + payload["device_token"]
    headers = provider.client.kwargs["headers"]
    assert headers["apns-topic"] == "co.hartl.HermesApple" and headers["apns-push-type"] == "alert"
    signed = headers["authorization"].split(" ")[1]
    assert jwt.decode(signed, key.public_key(), algorithms=["ES256"])["iss"] == "TEAM123456"
    packet = provider.client.kwargs["json"]
    assert "encrypted notification" in packet["aps"]["alert"]["body"]
    assert packet["aps"]["mutable-content"] == 1
    assert packet["klaudimero_encrypted"] == payload["encrypted"]
    assert "profile_id" not in json.dumps(packet) and "kind" not in json.dumps(packet)
    assert "appearance" not in json.dumps(packet) and "device_token" not in json.dumps(packet)
    assert len(json.dumps(packet).encode()) < 4096
    payload["environment"] = "sandbox"
    provider.send(Push(**payload))
    assert provider.client.url.startswith("https://api.sandbox.push.apple.com/")


def test_invalid_device_can_be_removed_without_retrying_forever(tmp_path, payload):
    provider = Provider(); provider.send = lambda push: Response(410, "Unregistered")
    with TestClient(create_app(provider, directory=tmp_path, clients={"host": "k" * 32})) as client:
        client.headers["Authorization"] = "Bearer " + "k" * 32
        assert client.post("/v1/push", json=payload).status_code == 410


def test_plaintext_routes_and_mismatched_envelope_are_rejected(tmp_path, payload):
    provider = Provider()
    with TestClient(create_app(provider, directory=tmp_path, clients={"host": "k" * 32})) as client:
        client.headers["Authorization"] = "Bearer " + "k" * 32
        for body in [{**payload, "kind": "reply_ready"}, {**payload, "session_id": "private"},
                     {**payload, "event_id": "f" * 64}, {**payload, "encrypted": {**payload["encrypted"], "version": 1}}]:
            assert client.post("/v1/push", json=body).status_code == 422
        assert provider.calls == []

