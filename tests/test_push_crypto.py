import base64
import json
import time
import uuid

import pytest
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from klaudimero_plugin.push_crypto import ALGORITHM, SUITE, context, seal
from klaudimero_plugin.storage import Store


def destination():
    return {"protocol": 1, "server_id": str(uuid.uuid4()), "device_id": str(uuid.uuid4()),
            "profile_id": "private + 日本", "session_id": "secret-chat", "event_id": "d" * 64, "kind": "session_failed"}


def open_packet(envelope, recipient, signer):
    info = context(envelope["key_id"], envelope["event_id"])
    signed = json.loads(SUITE.decrypt(base64.b64decode(envelope["ciphertext"]), recipient, info=info))
    raw = base64.b64decode(signed["payload"])
    signer.verify(base64.b64decode(signed["signature"]), info + raw)
    return json.loads(raw)


def test_sealed_packet_hides_routing_and_authenticates_sender():
    key = x25519.X25519PrivateKey.generate(); signing = ed25519.Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode(); value = destination()
    encrypted = seal(public, signing, value)
    opened = open_packet(encrypted, key, signing.public_key())
    assert opened["destination"] == value and opened["expires_at"] - opened["issued_at"] == 3600
    assert all(field not in json.dumps(encrypted) for field in ["private +", "secret-chat", "session_failed", "profile_id", "device_id"])
    assert seal(public, signing, value)["ciphertext"] != encrypted["ciphertext"]
    with pytest.raises(InvalidSignature): open_packet(encrypted, key, ed25519.Ed25519PrivateKey.generate().public_key())
    with pytest.raises((InvalidTag, ValueError)): open_packet(encrypted, x25519.X25519PrivateKey.generate(), signing.public_key())
    with pytest.raises((InvalidTag, ValueError)): open_packet({**encrypted, "event_id": "e" * 64}, key, signing.public_key())
    raw = bytearray(base64.b64decode(encrypted["ciphertext"])); raw[-1] ^= 1
    with pytest.raises((InvalidTag, ValueError)): open_packet({**encrypted, "ciphertext": base64.b64encode(raw).decode()}, key, signing.public_key())


def test_api_requires_public_encryption_key_without_plaintext_fallback(client):
    base = "/api/plugins/klaudimero"
    data = {"device_token": "f" * 64, "environment": "sandbox", "sessions": []}
    assert client.put(base + "/devices/" + str(uuid.uuid4()), json=data).status_code == 422
    caps = client.get(base + "/capabilities").json()
    assert caps["push_encryption"] == ALGORITHM and len(base64.b64decode(caps["push_signing_key"])) == 32
    for value in [None, [], {"version": 1, "public_key": "a"}, {"version": 2, "public_key": "not base64"},
                  {"version": True, "public_key": "a"}, {"version": 2, "public_key": base64.b64encode(bytes(32)).decode()},
                  {"version": 2, "public_key": base64.b64encode(b"\x01" + bytes(31)).decode()}]:
        assert client.put(base + "/devices/" + str(uuid.uuid4()), json={**data, "encryption": value}).status_code == 422


def test_outbox_encrypts_once_and_legacy_device_never_queues(tmp_path):
    store = Store(tmp_path); sessions = [{"profile_id": "default", "session_id": "private-chat"}]
    store.register_device("owner", str(uuid.uuid4()), "a" * 64, "production", sessions)
    store.enqueue("default", "private-chat", "turn", "reply_ready")
    assert store.claim() is None
    private = x25519.X25519PrivateKey.generate(); device = str(uuid.uuid4())
    public = base64.b64encode(private.public_key().public_bytes_raw()).decode()
    store.register_device("owner", device, "b" * 64, "production", sessions, public)
    store.enqueue("default", "private-chat", "turn", "reply_ready")
    first = store.claim(); packet = json.loads(first["payload"])
    assert "private-chat" not in first["payload"] and "reply_ready" not in first["payload"]
    assert open_packet(packet["encrypted"], private, store.push_signing_key().public_key())["destination"]["device_id"] == device
    store.settle(first["id"], retry=True)
    with store.connection() as db: db.execute("UPDATE outbox SET next_attempt=0")
    assert store.claim()["payload"] == first["payload"]
    assert Store(tmp_path).push_signing_public_key == store.push_signing_public_key


def test_key_rotation_retires_previous_ciphertext(tmp_path):
    store = Store(tmp_path); device = str(uuid.uuid4()); sessions = [{"profile_id": "default", "session_id": "chat"}]
    public = lambda: base64.b64encode(x25519.X25519PrivateKey.generate().public_key().public_bytes_raw()).decode()
    store.register_device("owner", device, "b" * 64, "production", sessions, public())
    store.enqueue("default", "chat", "turn", "reply_ready"); old = store.claim()
    store.register_device("owner", device, "b" * 64, "production", sessions, public())
    store.settle(old["id"], retry=True)
    assert store.claim() is None


def test_old_plaintext_queue_is_retired_even_from_a_legacy_worker(tmp_path):
    store = Store(tmp_path); device = str(uuid.uuid4())
    store.register_device("owner", device, "b" * 64, "production", [])
    with store.connection() as db:
        db.execute("INSERT INTO outbox (id,owner,device,payload) VALUES (?,?,?,?)", ("old", "owner", device,
                   json.dumps({"device_token": "b" * 64, "profile_id": "private", "session_id": "private", "kind": "reply_ready"})))
    assert store.claim() is None
    with store.connection() as db: assert db.execute("SELECT payload FROM outbox").fetchone()[0] == "{}"


def test_message_preview_is_signed_encrypted_and_bounded():
    from klaudimero_plugin.notification_preview import preview
    key = x25519.X25519PrivateKey.generate(); signing = ed25519.Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    value = preview('Private reply\n日本 🙂 ' * 100)
    assert value['body'].startswith('Private reply 日本 🙂') and len(value['body'].encode()) <= 512
    encrypted = seal(public, signing, destination(), notification=value)
    opened = open_packet(encrypted, key, signing.public_key())
    assert opened['notification'] == value
    assert 'Private reply' not in json.dumps(encrypted) and 'notification' not in json.dumps(encrypted)
    with pytest.raises(InvalidSignature): open_packet(encrypted, key, ed25519.Ed25519PrivateKey.generate().public_key())


def test_preview_respects_existing_ciphertext_and_apns_budgets():
    key = x25519.X25519PrivateKey.generate(); signing = ed25519.Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    value = {**destination(), 'profile_id': '日'*120, 'session_id': '本'*120}
    packet = seal(public, signing, value, notification={'title': 'Klaudimero', 'body': '"'*512})
    assert len(base64.b64decode(packet['ciphertext'])) <= 2400
    wire = {'aps': {'alert': {'title': 'Klaudimero', 'body': 'Open Klaudimero to view an encrypted notification.'},
                    'mutable-content': 1, 'sound': 'default'}, 'klaudimero_encrypted': packet}
    assert len(json.dumps(wire).encode()) <= 4096
    opened = open_packet(packet, key, signing.public_key())
    assert opened['destination'] == value
    assert not opened.get('notification') or len(opened['notification']['body'].encode()) < 512
