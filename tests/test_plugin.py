import concurrent.futures
import json
import uuid
import pytest
from klaudimero_plugin.contracts import appearance, Problem
from klaudimero_plugin.hooks import normalize, register
from klaudimero_plugin.storage import Store

BASE = "/api/plugins/klaudimero"


def put(client, avatar, revision=0, request=None, profile="work + 日本"):
    return client.put(BASE + "/avatars/" + profile, json={"expected_revision": revision,
        "request_id": request or str(uuid.uuid4()), "appearance": avatar})


def test_requires_host_verified_identity(client):
    client.headers.pop("Authorization")
    assert client.get(BASE + "/capabilities").status_code == 401


def test_capabilities_and_account_isolation(client, avatar):
    first = client.get(BASE + "/capabilities").json()
    assert first["protocol"] == 1 and not first["push_relay_configured"]
    assert put(client, avatar).status_code == 200
    assert client.get(BASE + "/avatars").json()["avatars"]["work + 日本"]["appearance"] == avatar
    client.headers["Authorization"] = "Bearer bob"
    second = client.get(BASE + "/capabilities").json()
    assert first["server_id"] == second["server_id"]
    assert first["account_id"] != second["account_id"]
    assert client.get(BASE + "/avatars").json()["avatars"] == {}
    assert put(client, avatar).status_code == 200


def test_two_devices_cas_and_uncertain_response_retry(client, avatar):
    request = str(uuid.uuid4())
    first = put(client, avatar, request=request)
    assert first.status_code == 200 and first.json()["revision"] == 1
    assert put(client, avatar, request=request).json() == first.json()
    assert put(client, avatar).status_code == 409
    changed = {**avatar, "color": "blue"}
    assert put(client, changed, request=request).json()["detail"] == "request_id_reused"
    assert put(client, changed, revision=1).json()["revision"] == 2
    assert client.get(BASE + "/avatars").headers["Cache-Control"] == "no-store"


def test_concurrent_writers_only_one_can_commit(tmp_path, avatar):
    store = Store(tmp_path)
    def write(_):
        try:
            store.put_avatar("owner", "default", 0, str(uuid.uuid4()), avatar)
            return 200
        except Problem as error:
            return error.status
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(write, range(8)))
    assert outcomes.count(200) == 1 and outcomes.count(409) == 7


def test_validation_does_not_echo_device_token(client):
    token = "a" * 64
    response = client.put(BASE + "/devices/" + str(uuid.uuid4()), json={
        "device_token": token, "environment": "unknown", "sessions": [], "arbitrary": "not allowed"})
    assert response.status_code == 422 and token not in response.text


@pytest.mark.parametrize("field,value", [("expression", []), ("cheeks", 1), ("accessory", "unknown")])
def test_malformed_chick_never_causes_server_error(avatar, field, value):
    avatar["chick"][field] = value
    with pytest.raises(Problem):
        appearance(avatar)


def test_photo_and_symbol_roundtrip(client, avatar):
    import base64
    avatar["image"] = base64.b64encode(b"synthetic image bytes").decode()
    avatar["style"] = "photo"
    assert put(client, avatar).json()["appearance"] == avatar
    avatar["style"] = "symbol"
    assert put(client, avatar, revision=1).json()["appearance"] == avatar


def test_registration_token_rotation_account_isolation_and_revocation(client, tmp_path):
    device = str(uuid.uuid4())
    data = {"device_token": "a" * 64, "environment": "sandbox", "sessions": [{"profile_id": "developer", "session_id": "chat"}]}
    assert client.put(BASE + "/devices/" + device, json=data).status_code == 200
    data["device_token"] = "b" * 64
    assert client.put(BASE + "/devices/" + device, json=data).status_code == 200
    store = Store(tmp_path / "plugin")
    store.enqueue("default", "chat", "turn0", "reply_ready")
    assert store.claim() is None
    store.enqueue("developer", "chat", "turn1", "reply_ready")
    event = store.claim()
    assert json.loads(event["payload"])["device_token"] == "b" * 64
    assert "text" not in json.loads(event["payload"])
    client.headers["Authorization"] = "Bearer bob"
    assert client.delete(BASE + "/devices/" + device).status_code == 200
    store.settle(event["id"], retry=True)
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM devices").fetchone()[0] == 1
    client.headers["Authorization"] = "Bearer alice"
    assert client.delete(BASE + "/devices/" + device).status_code == 200
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM devices").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0


def test_repeated_hooks_are_deduplicated_and_subagents_are_quiet(tmp_path):
    store = Store(tmp_path)
    store.register_device("owner", str(uuid.uuid4()), "a" * 64, "production", [{"profile_id": "default", "session_id": "chat"}])
    event = normalize("default", {"session_id": "chat", "turn_id": "turn", "completed": True})
    store.enqueue(*event); store.enqueue(*event)
    row = store.claim(); assert row is not None
    store.settle(row["id"])
    store.enqueue(*event)
    assert store.claim() is None
    for extra in [{"interrupted": True}, {"platform": "subagent"}, {"parent_session_id": "parent"}, {"turn_id": ""}]:
        assert normalize("default", {"session_id": "chat", "turn_id": "turn", "completed": True, **extra}) is None


def test_outbox_claim_excludes_another_worker_and_retries_same_id(tmp_path):
    store = Store(tmp_path)
    store.register_device("owner", str(uuid.uuid4()), "a" * 64, "production", [{"profile_id": "default", "session_id": "chat"}])
    store.enqueue("default", "chat", "turn", "reply_ready")
    first = store.claim()
    assert first and store.claim() is None
    store.settle(first["id"], retry=True)
    assert store.claim() is None
    with store.connection() as db:
        db.execute("UPDATE outbox SET next_attempt=0")
    assert store.claim()["id"] == first["id"]
    store.settle(first["id"], drop_device=True)
    assert store.claim() is None
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM devices").fetchone()[0] == 0


def test_metadata_revision_changes_without_exposing_image_data(client, avatar):
    assert client.get(BASE + "/capabilities").json()["avatar_revision"] == 0
    put(client, avatar)
    capabilities = client.get(BASE + "/capabilities").json()
    assert capabilities["avatar_revision"] == 1
    assert "appearance" not in capabilities and "device_token" not in capabilities


def test_old_token_failure_cannot_revoke_rotated_registration(tmp_path):
    store = Store(tmp_path)
    device = str(uuid.uuid4())
    sessions = [{"profile_id": "default", "session_id": "chat"}]
    store.register_device("owner", device, "a" * 64, "sandbox", sessions)
    store.enqueue("default", "chat", "old-turn", "reply_ready")
    old = store.claim()
    store.register_device("owner", device, "b" * 64, "production", sessions)
    store.settle(old["id"], drop_device=True)
    store.settle(old["id"], retry=True)
    assert store.claim() is None
    with store.connection() as db:
        assert db.execute("SELECT token FROM devices").fetchone()[0] == "b" * 64
    store.enqueue("default", "chat", "old-turn", "reply_ready")
    assert store.claim() is None
    store.enqueue("default", "chat", "new-turn", "reply_ready")
    current = json.loads(store.claim()["payload"])
    assert current["device_token"] == "b" * 64 and current["environment"] == "production"


def test_hook_registration_is_network_free_and_forward_compatible():
    class Context:
        profile_name = "default"
        def register_hook(self, name, callback):
            self.name, self.callback = name, callback
        def on_unload(self, callback):
            self.unload = callback
    ctx = Context(); register(ctx)
    assert ctx.name == "on_session_end"
    ctx.callback(completed=False, future_field="ignored")
    ctx.unload()

