"""Run with HERMES_TEST_SOURCE pointing at the supported Hermes checkout."""
import asyncio
import concurrent.futures
import hashlib
import os
from pathlib import Path
import sys
import uuid

import pytest

source = os.environ.get("HERMES_TEST_SOURCE")
if not source:
    pytest.skip("Set HERMES_TEST_SOURCE for real Hermes channel integration", allow_module_level=True)
sys.path.insert(0, source)

from hermes_state_registry import acquire, release_or_close, close_all_under
from klaudimero_plugin import channel
from klaudimero_plugin.contracts import Problem
from klaudimero_plugin.delivery import Sender
from klaudimero_plugin.storage import Store


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir(); (home / "config.yaml").write_text("{}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("KLAUDIMERO_DATA_DIR", str(tmp_path / "companion"))
    for key in ["HERMES_PROFILE", "HERMES_PROFILE_NAME", "KLAUDIMERO_HOME_CHANNEL"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Sender, "start", lambda self: None)
    work = home / "profiles/work"
    work.mkdir(parents=True); (work / "config.yaml").write_text("{}\n")
    dbs = {}
    for profile, path in [("default", home), ("work", work)]:
        db = acquire(path / "state.db")
        sid = str(uuid.uuid4())
        db.create_session(sid, "desktop")
        db.set_session_title(sid, "Bot Chat")
        db.set_session_hidden(sid, True)
        db.append_message(sid, "user", "Existing question " + profile)
        db.append_message(sid, "assistant", "Existing reply " + profile)
        dbs[profile] = (db, sid)
    yield home, dbs
    for db, _ in dbs.values(): release_or_close(db)
    close_all_under(home)
    from klaudimero_plugin.api import service
    service.cache_clear()


def test_standalone_cron_delivers_only_to_selected_main_chat(homes):
    _, dbs = homes
    response = asyncio.run(channel.standalone_send(None, "bot:work", "Your trip update is ready."))
    assert response["success"], response
    work, sid = dbs["work"]
    assert work.get_messages(sid)[-1]["role"] == "assistant"
    assert work.get_messages(sid)[-1]["content"] == "Your trip update is ready."
    default, default_sid = dbs["default"]
    assert len(default.get_messages(default_sid)) == 2
    with Store().connection() as conn:
        row = conn.execute("SELECT status,content FROM channel_messages").fetchone()
        assert tuple(row) == ("notified", "")


def test_repeated_receipt_concurrent_senders_and_identical_distinct_messages(homes):
    _, dbs = homes
    def send(_): return channel.submit("bot", "Scheduled update", metadata={"delivery_id": "job-run-1"})
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(send, range(4)))
    assert len({r["message_id"] for r in responses}) == 1
    channel.drain(Store())
    db, sid = dbs["default"]
    assert len(db.get_messages(sid)) == 3
    with pytest.raises(Problem):
        channel.submit("bot", "Different result", metadata={"delivery_id": "job-run-1"})
    channel.submit("bot", "Scheduled update")
    channel.submit("bot", "Scheduled update")
    assert len(db.get_messages(sid)) == 5


def test_busy_canonical_writer_defers_and_recovery_does_not_duplicate(homes):
    _, dbs = homes
    db, sid = dbs["default"]
    assert db.try_acquire_session_turn_lease(sid, "owned-test-turn")
    try:
        response = channel.submit("bot", "Queued while busy")
        assert response["success"]
        assert len(db.get_messages(sid)) == 2
        with Store().connection() as conn:
            assert conn.execute("SELECT status FROM channel_messages").fetchone()[0] == "queued"
    finally:
        db.release_session_turn_lease(sid, "owned-test-turn")
    with Store().connection() as conn: conn.execute("UPDATE channel_messages SET next_attempt=0")
    channel.drain(Store())
    assert db.get_messages(sid)[-1]["content"] == "Queued while busy"
    # Reproduce death after the canonical append, before the plugin receipt commit.
    with Store().connection() as conn:
        conn.execute("UPDATE channel_messages SET status='queued',content='Queued while busy',next_attempt=0")
    channel.drain(Store())
    assert len(db.get_messages(sid)) == 3


def test_unknown_archived_missing_chat_and_unsupported_delivery_fail_honestly(homes):
    _, dbs = homes
    for target in ["bot:../../escape", "bot:unknown", "/tmp/state.db", "work"]:
        with pytest.raises(Problem): channel.submit(target, "private")
    db, sid = dbs["default"]
    db.set_session_archived(sid, True)
    with pytest.raises(Problem): channel.submit("bot", "archived")
    assert not asyncio.run(channel.standalone_send(None, "bot:work", "file", media_files=["secret.txt"])).get("success")
    assert not asyncio.run(channel.standalone_send(None, "bot:work", "thread", thread_id="a")).get("success")
    assert not asyncio.run(channel.standalone_send(None, "bot:work", "")).get("success")


def test_encrypted_notification_is_scoped_and_contains_no_delivery_text(homes):
    import base64,json
    from cryptography.hazmat.primitives.asymmetric import x25519
    _, dbs = homes
    store = Store(); _, sid = dbs["work"]
    pub = base64.b64encode(x25519.X25519PrivateKey.generate().public_key().public_bytes_raw()).decode()
    store.register_device("owner", str(uuid.uuid4()), "", "", [{"profile_id": "work", "session_id": sid}], pub, str(uuid.uuid4()))
    result = channel.submit("bot:work", "Private job output stays on Hermes")
    packet = json.loads(store.claim()["payload"])
    assert set(packet) == {"grant_id", "event_id", "encrypted"}
    assert "Private job output" not in json.dumps(packet)
    channel.drain(store)
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    assert result["success"]


def test_real_channels_catalog_cron_home_and_gateway_adapter(homes, monkeypatch):
    from types import SimpleNamespace
    from gateway.platform_registry import PlatformEntry, platform_registry
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.config_env import _enable_plugin_platform
    from hermes_cli.web_server_messaging import _build_catalog_entry
    context = SimpleNamespace(register_platform=lambda **kwargs: values.update(kwargs))
    values = {}; channel.register(context)
    entry = PlatformEntry(**values)
    previous = platform_registry.snapshot_registration("klaudimero", scope=None)
    platform_registry.register(entry)
    try:
        catalog = _build_catalog_entry("klaudimero", entry)
        assert catalog["name"] == "Klaudimero" and not catalog["required_env"] and not catalog["env_vars"]
        config = GatewayConfig(); _enable_plugin_platform(config, entry)
        p = Platform("klaudimero")
        assert config.platforms[p].enabled and config.get_home_channel(p).chat_id == "bot"
        disabled = GatewayConfig.from_dict({"platforms": {"klaudimero": {"enabled": False}}})
        # load_yaml_layer sets this marker for an explicit YAML enabled flag.
        disabled.platforms[p].extra["_enabled_explicit"] = True
        _enable_plugin_platform(disabled, entry)
        assert not disabled.platforms[p].enabled
        from cron.scheduler_delivery import _is_known_delivery_platform
        assert _is_known_delivery_platform("klaudimero")
        async def run():
            adapter = entry.adapter_factory(config.platforms[p])
            assert await adapter.connect()
            try:
                channels = await adapter.list_channels()
                assert {c["id"] for c in channels} == {"bot:default", "bot:work"}
                result = await adapter.send("bot:work", "Live gateway update")
                assert result.success, result.error
                rejected = await adapter.send("bot:work", "Unwanted threaded update", metadata={"thread_id": "thread-1"})
                assert not rejected.success
            finally: await adapter.disconnect()
        asyncio.run(run())
        db, sid = homes[1]["work"]
        assert db.get_messages(sid)[-1]["content"] == "Live gateway update"
        # Exercise the same generic dispatcher used by standalone cron jobs.
        from tools import send_message_tool
        monkeypatch.setattr(send_message_tool, "_live_adapter", lambda platform: (None, None))
        result = asyncio.run(send_message_tool._send_to_platform(p, config.platforms[p], "bot:work", "Cron dispatcher update"))
        assert result.get("success"), result
        assert db.get_messages(sid)[-1]["content"] == "Cron dispatcher update"
        monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
        assert send_message_tool._resolve_tool_target("klaudimero:new:work") == ("klaudimero", "new:work", None, None)
        import json
        routed = json.loads(send_message_tool.send_message_tool({"action":"send", "target":"klaudimero:new:work",
                            "message":"Prepare the launch review\n\nResolve the remaining decisions."}))
        assert routed.get("success"), routed
        assert db.get_session_by_title("Prepare the launch review") is not None
    finally:
        current = platform_registry.snapshot_registration("klaudimero", scope=None)
        platform_registry.restore_registration("klaudimero", current, previous, scope=None)


def test_installed_plugin_is_discovered_by_real_web_channels_and_cron():
    # Only the test uses Doctor's isolated/blocked-network runtime. Production
    # registration goes through ctx.register_platform, never private host state.
    from hermes_cli.plugin_dev import _doctor_runtime
    from gateway.platform_registry import platform_registry
    from hermes_cli.web_server_messaging import _messaging_platform_catalog
    from gateway.config import load_gateway_config, Platform
    from cron.scheduler_delivery import cron_delivery_targets
    with _doctor_runtime(Path(__file__).resolve().parents[1]) as loaded:
        assert "klaudimero" in loaded.manager._plugin_platform_names
        entry = platform_registry.get("klaudimero")
        assert entry is not None and entry.standalone_sender_fn is not None
        cards = _messaging_platform_catalog()
        card = next(card for card in cards if card["id"] == "klaudimero")
        assert card["name"] == "Klaudimero" and not card["required_env"]
        config = load_gateway_config()
        platform = Platform("klaudimero")
        assert config.platforms[platform].enabled
        assert config.get_home_channel(platform).chat_id == "bot"
        targets = cron_delivery_targets()
        assert next(t for t in targets if t["id"] == "klaudimero")["home_target_set"]


def test_queued_messages_do_not_overtake_earlier_delivery(homes):
    _, dbs = homes; db, sid = dbs["default"]
    assert db.try_acquire_session_turn_lease(sid, "owned-test-turn")
    try:
        channel.submit("bot", "First update")
        channel.submit("bot", "Second update")
    finally:
        db.release_session_turn_lease(sid, "owned-test-turn")
    # The second row is due immediately but cannot pass the deferred first row.
    channel.drain(Store())
    assert len(db.get_messages(sid)) == 2
    with Store().connection() as conn: conn.execute("UPDATE channel_messages SET next_attempt=0")
    channel.drain(Store()); channel.drain(Store())
    assert [m["content"] for m in db.get_messages(sid)][-2:] == ["First update", "Second update"]


def test_current_named_profile_default_and_compression_subscriptions(homes, monkeypatch):
    import base64,json
    from cryptography.hazmat.primitives.asymmetric import x25519
    home, dbs = homes
    db, root = dbs["work"]
    child = str(uuid.uuid4())
    db.create_session(child, "desktop", parent_session_id=root)
    db.end_session(root, "compression")
    assert db.get_compression_tip(root) == child
    store = Store()
    pub = base64.b64encode(x25519.X25519PrivateKey.generate().public_key().public_bytes_raw()).decode()
    store.register_device("owner", str(uuid.uuid4()), "", "", [{"profile_id": "work", "session_id": root}], pub, str(uuid.uuid4()))
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles/work"))
    assert channel.profile_target("bot")[0] == "work"
    channel.submit("bot", "After compression")
    assert db.get_messages(child)[-1]["content"] == "After compression"
    assert len(dbs["default"][0].get_messages(dbs["default"][1])) == 2
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1


def test_profile_pin_replacement_requires_explicit_update(homes, client, monkeypatch):
    from types import SimpleNamespace
    from klaudimero_plugin import api
    home, _ = homes
    manifest = home / "profiles/work/plugins/klaudimero/plugin.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('name: klaudimero\nversion: "0.3.0"\n')
    calls = []
    monkeypatch.setattr(api.shutil, "which", lambda name: "/test/bin/hermes")
    monkeypatch.setattr(api.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    body = {"profile_ids": ["work"], "revision": "a" * 40}
    result = client.post("/api/plugins/klaudimero/profiles/enable", json=body)
    assert result.status_code == 200 and not result.json()["profiles"][0]["enabled"] and not calls
    result = client.post("/api/plugins/klaudimero/profiles/enable", json={**body, "update": True, "approve_python_dependencies": True})
    assert result.status_code == 200 and result.json()["profiles"][0]["enabled"]
    assert calls[0][0] == ["/test/bin/hermes", "-p", "work", "plugins", "install", api.REPOSITORY, "--ref", "a" * 40, "--enable", "--yes-deps", "--force"]
    assert "shell" not in calls[0][1]
    assert client.post("/api/plugins/klaudimero/profiles/enable", json={**body, "profile_ids": ["../other"], "update": True}).status_code == 404
    assert len(calls) == 1


def test_update_requires_approval_verifies_pin_and_keeps_retry_receipt(homes, client, monkeypatch):
    from types import SimpleNamespace
    from klaudimero_plugin import updates
    calls = []; pin = "a" * 40
    monkeypatch.setattr(updates.shutil, "which", lambda tool: "/test/bin/" + tool)
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=(pin + "\n").encode())
    monkeypatch.setattr(updates.subprocess, "run", run)
    body = {"request_id": str(uuid.uuid4()), "profile_id": "work", "revision": pin}
    assert client.post("/api/plugins/klaudimero/updates", json=body).status_code == 409
    assert not calls
    body["approve_python_dependencies"] = True
    response = client.post("/api/plugins/klaudimero/updates", json=body)
    assert response.status_code == 200 and response.json()["state"] == "complete"
    assert calls[0][0] == ["/test/bin/hermes", "-p", "work", "plugins", "install", updates.REPOSITORY,
                          "--ref", pin, "--enable", "--force", "--yes-deps"]
    assert all("shell" not in kwargs for args, kwargs in calls)
    assert client.post("/api/plugins/klaudimero/updates", json=body).json() == response.json()
    assert len(calls) == 2
    assert client.post("/api/plugins/klaudimero/updates", json={**body, "revision": "b" * 40}).status_code == 409
    client.headers["Authorization"] = "Bearer bob"
    assert client.get("/api/plugins/klaudimero/updates/" + body["request_id"]).status_code == 404
    assert client.post("/api/plugins/klaudimero/updates", json=body).status_code == 409


def test_update_refusal_or_unverified_tree_never_reports_success(homes, client, monkeypatch):
    from types import SimpleNamespace
    from klaudimero_plugin import updates
    monkeypatch.setattr(updates.shutil, "which", lambda tool: "/test/bin/" + tool)
    for failure in [True, False]:
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=1 if failure else 0, stdout=b"private credential output")
        monkeypatch.setattr(updates.subprocess, "run", run)
        body = {"request_id": str(uuid.uuid4()), "profile_id": "default", "revision": "c" * 40,
                "approve_python_dependencies": True}
        result = client.post("/api/plugins/klaudimero/updates", json=body)
        assert result.json()["state"] == "failed" and not result.json()["restart_required"]
        assert "private credential" not in result.text
        count = len(calls)
        assert client.post("/api/plugins/klaudimero/updates", json=body).json() == result.json()
        assert len(calls) == count


def test_real_hermes_forced_http_install_cannot_carry_dependency_approval(tmp_path, monkeypatch):
    from unittest.mock import patch
    from hermes_cli.plugins_transaction import publish_plugin
    from hermes_cli.plugins_cmd import PluginOperationError
    from hermes_cli.web_models import _AgentPluginInstallBody
    target = tmp_path / "installed"; target.mkdir()
    staged = tmp_path / "candidate"; staged.mkdir()
    (staged / "plugin.yaml").write_text('name: klaudimero\nversion: "0.4.1"\npython_dependencies:\n  - "cryptography>=50,<51"\n')
    body = _AgentPluginInstallBody(identifier=channel.__name__, force=True, enable=True)
    with patch("pm.workspace.enabled_plugin_dirs", return_value=[target.resolve()]), patch("pm.client.sync_venv") as sync:
        # Real publish and consent logic; only final PM execution is substituted.
        with pytest.raises(PluginOperationError, match="dependency install skipped"):
            publish_plugin(staged, target, {}, {}, require_consent=True, assume_consent=body.enable and not body.force)
        assert not sync.called
        publish_plugin(staged, target, {}, {}, require_consent=True, assume_consent=True)
        assert sync.call_args.kwargs["explicit"] is True


def test_hook_preview_reads_only_visible_tail_from_owning_profile(homes):
    from klaudimero_plugin.notification_preview import session_preview
    _, dbs = homes
    db, sid = dbs['work']
    db.append_message(sid, 'user', 'Current request')
    db.append_message(sid, 'assistant', 'SECRET REASONING', display_kind='reasoning')
    db.append_message(sid, 'tool', 'SECRET TOOL PAYLOAD', tool_name='terminal')
    db.append_message(sid, 'assistant', 'This is the actual final answer.')
    assert session_preview('work', sid, 'reply_ready')['body'] == 'This is the actual final answer.'
    assert session_preview('default', sid, 'reply_ready') is None
    assert session_preview('work', sid, 'session_failed') is None
    db.append_message(sid, 'user', 'A newer request must not reuse the old answer')
    assert session_preview('work', sid, 'reply_ready') is None


def test_channel_retry_keeps_its_exact_preview_after_transcript_moves_on(homes, monkeypatch):
    import base64
    import json
    from cryptography.hazmat.primitives.asymmetric import x25519
    from klaudimero_plugin.push_crypto import SUITE, context
    _, dbs = homes
    db, sid = dbs['work']; store = Store()
    private = x25519.X25519PrivateKey.generate()
    store.register_device('owner', str(uuid.uuid4()), 'a'*64, 'production',
        [{'profile_id':'work','session_id':sid}], base64.b64encode(private.public_key().public_bytes_raw()).decode())
    enqueue = Store.enqueue
    def fail(*a, **kw): raise RuntimeError('Simulated failure after canonical append')
    monkeypatch.setattr(Store, 'enqueue', fail)
    channel.submit('bot:work', 'Exact scheduled update: 日本')
    with store.connection() as conn:
        row = conn.execute('SELECT * FROM channel_messages').fetchone()
        assert row['status']=='written' and row['content']==''
        assert json.loads(row['notification_preview'])['body']=='Exact scheduled update: 日本'
        conn.execute('UPDATE channel_messages SET next_attempt=0')
    db.append_message(sid, 'assistant', 'Another unrelated answer that must not leak into the push')
    monkeypatch.setattr(Store, 'enqueue', enqueue)
    channel.drain(store)
    packet = json.loads(store.claim()['payload'])['encrypted']
    signed = json.loads(SUITE.decrypt(base64.b64decode(packet['ciphertext']), private,
                                     info=context(packet['key_id'], packet['event_id'])))
    content = json.loads(base64.b64decode(signed['payload']))
    assert content['notification']['body']=='Exact scheduled update: 日本'
    with store.connection() as conn:
        row=conn.execute('SELECT * FROM channel_messages').fetchone()
        assert row['status']=='notified' and row['notification_preview'] is None


def test_new_delivery_creates_a_visible_titled_chat_in_the_selected_profile(homes):
    _, dbs = homes
    result = channel.submit("new:work", "Prepare for tomorrow's meeting\n\nReview the three open decisions.", metadata={"delivery_id": "proactivity-1"})
    assert result["success"]
    with Store().connection() as conn:
        sid = conn.execute("SELECT session_id FROM new_chat_messages").fetchone()[0]
    db, main = dbs["work"]
    assert sid != main
    session = db.get_session(sid)
    assert session["title"] == "Prepare for tomorrow's meeting"
    assert session["source"] == "delivery" and not session["hidden"]
    assert db.get_messages(sid)[0]["content"] == "Review the three open decisions."
    assert len(db.get_messages(main)) == 2
    assert len(dbs["default"][0].get_messages(dbs["default"][1])) == 2


def test_older_workers_cannot_claim_a_queued_new_chat_delivery(homes, monkeypatch):
    with monkeypatch.context() as pending:
        pending.setattr(channel, "drain", lambda store: None)
        result = channel.submit("new", "Keep this in its own chat\n\nA queued suggestion.")
    with Store().connection() as conn:
        assert conn.execute("SELECT count(*) FROM channel_messages WHERE status!='notified'").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM new_chat_messages WHERE id=?", (result["message_id"],)).fetchone()[0] == "queued"
    channel.drain(Store())
    with Store().connection() as conn:
        assert conn.execute("SELECT status FROM new_chat_messages WHERE id=?", (result["message_id"],)).fetchone()[0] == "notified"


def test_new_chat_receipt_and_crash_recovery_do_not_create_duplicate_chats(homes):
    _, dbs = homes
    content = "Close the travel planning loop\n\nConfirm the remaining booking."
    response = channel.submit("new", content, metadata={"delivery_id": "same-request"})
    repeated = channel.submit("new", content, metadata={"delivery_id": "same-request"})
    assert response == repeated
    with Store().connection() as conn:
        sid = conn.execute("SELECT session_id FROM new_chat_messages").fetchone()[0]
        conn.execute("UPDATE new_chat_messages SET status='queued',content=?,next_attempt=0", (content,))
    channel.drain(Store())
    db, _ = dbs["default"]
    assert len(db.get_messages(sid)) == 1
    with Store().connection() as conn:
        assert conn.execute("SELECT count(*) FROM new_chat_messages").fetchone()[0] == 1
    second = channel.submit("new", content)
    with Store().connection() as conn:
        sid2 = conn.execute("SELECT session_id FROM new_chat_messages WHERE id=?", (second["message_id"],)).fetchone()[0]
    assert sid2 != sid
    assert db.get_session_title(sid2).startswith("Close the travel planning loop")
    assert db.get_session_title(sid2) != db.get_session_title(sid)


def test_new_chat_push_opens_the_new_chat_only_for_the_same_bot_recipients(homes):
    import base64, json
    from cryptography.hazmat.primitives.asymmetric import x25519
    from klaudimero_plugin.push_crypto import SUITE, context
    _, dbs = homes
    store = Store(); _, main = dbs["work"]
    recipient = x25519.X25519PrivateKey.generate()
    public = base64.b64encode(recipient.public_key().public_bytes_raw()).decode()
    device = str(uuid.uuid4())
    store.register_device("owner", device, "", "", [{"profile_id": "work", "session_id": main}], public, str(uuid.uuid4()))
    for profile, sid in [("default", main), ("work", "unrelated-chat")]:
        store.register_device("owner", str(uuid.uuid4()), "", "", [{"profile_id": profile, "session_id": sid}], public, str(uuid.uuid4()))
    result = channel.submit("new:work", "A private follow-up title\n\nPrivate suggestion content")
    with store.connection() as conn:
        sid = conn.execute("SELECT session_id FROM new_chat_messages WHERE id=?", (result["message_id"],)).fetchone()[0]
        assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
    envelope = json.loads(store.claim()["payload"])
    assert "Private suggestion" not in json.dumps(envelope)
    encrypted = envelope["encrypted"]
    info = context(encrypted["key_id"], encrypted["event_id"])
    signed = json.loads(SUITE.decrypt(base64.b64decode(encrypted["ciphertext"]), recipient, info=info))
    payload = base64.b64decode(signed["payload"])
    store.push_signing_key().public_key().verify(base64.b64decode(signed["signature"]), info + payload)
    content = json.loads(payload)
    destination = content["destination"]
    assert destination["session_id"] == sid and sid != main
    assert destination["profile_id"] == "work" and destination["device_id"] == device
    assert content["notification"] == {"title": "A private follow-up title", "body": "Private suggestion content"}


def test_new_chat_retry_preserves_its_preview_after_another_message(homes, monkeypatch):
    import json
    _, dbs = homes
    store = Store()
    enqueue = Store.enqueue_delivery
    with monkeypatch.context() as failing:
        def fail(*args, **kwargs):
            raise RuntimeError("Simulated interruption after writing the delivery chat")
        failing.setattr(Store, "enqueue_delivery", fail)
        channel.submit("new:work", "Resolve the open decisions\n\nConfirm the final booking.")
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM new_chat_messages").fetchone()
        sid = row["session_id"]
        expected = {"title": "Resolve the open decisions", "body": "Confirm the final booking."}
        assert row["status"] == "written" and row["content"] == ""
        assert json.loads(row["notification_preview"]) == expected
        conn.execute("UPDATE new_chat_messages SET next_attempt=0")
    dbs["work"][0].append_message(sid, "assistant", "A later answer must not replace the original preview")
    seen = []
    def capture(self, *args, **kwargs):
        seen.append(kwargs["notification"])
        return enqueue(self, *args, **kwargs)
    monkeypatch.setattr(Store, "enqueue_delivery", capture)
    channel.drain(store)
    assert seen == [expected]
    with store.connection() as conn:
        row = conn.execute("SELECT * FROM new_chat_messages").fetchone()
        assert row["status"] == "notified" and row["notification_preview"] is None


def test_new_chat_queue_without_preview_column_upgrades_without_losing_delivery(homes, monkeypatch):
    store = Store()
    with monkeypatch.context() as pending:
        pending.setattr(channel, "drain", lambda store: None)
        result = channel.submit("new", "Keep the existing delivery\n\nStill waiting to be sent.")
    with store.connection() as conn:
        conn.execute("ALTER TABLE new_chat_messages DROP COLUMN notification_preview")
    upgraded = Store()
    channel.drain(upgraded)
    with upgraded.connection() as conn:
        row = conn.execute("SELECT * FROM new_chat_messages WHERE id=?", (result["message_id"],)).fetchone()
        assert row["status"] == "notified" and row["notification_preview"] is None
    db, _ = homes[1]["default"]
    assert db.get_session_title(row["session_id"]) == "Keep the existing delivery"
    assert db.get_messages(row["session_id"])[0]["content"] == "Still waiting to be sent."
