"""Klaudimero's local delivery channel; reply previews are encrypted before reaching APNs."""
import asyncio
import hashlib
import json
import time
import uuid

from .contracts import Problem
from .storage import Store


def profile_target(target):
    from hermes_cli.profiles import current_profile_name, get_profile_dir, list_profile_names
    current = current_profile_name(default="default")
    if target == "bot":
        profile = current
    elif isinstance(target, str) and target.startswith("bot:"):
        profile = target[4:]
    else:
        raise Problem(422, "Use bot or bot:<profile> as the Klaudimero target")
    if profile not in {"default", *list_profile_names()}:
        raise Problem(404, "Klaudimero target profile does not exist")
    return profile, get_profile_dir(profile)


def history(home):
    from hermes_state_registry import acquire
    if not (home / "state.db").is_file():
        raise Problem(409, "Open this bot in Klaudimero once before delivering to its main chat")
    return acquire(home / "state.db")


def canonical(db):
    row = db.get_session_by_title("Bot Chat")
    if not row:
        raise Problem(409, "Open this bot in Klaudimero once before delivering to its main chat")
    sid = db.get_compression_tip(row["id"]) or row["id"]
    tip = db.get_session(sid)
    if not tip or row.get("archived") or tip.get("archived"):
        raise Problem(409, "Restore this bot's archived main chat before delivering to it")
    return sid, db.get_compression_lineage(sid)


def submit(target, content, *, metadata=None):
    from hermes_state_registry import release_or_close
    profile, home = profile_target(target)
    if not isinstance(content, str) or not content.strip():
        raise Problem(422, "Klaudimero delivery needs a nonempty message")
    if len(content.encode()) > 262144:
        raise Problem(422, "Klaudimero delivery exceeds 256 KiB")
    db = history(home)
    try:
        canonical(db)  # Validate before acknowledging anything as accepted.
    finally:
        release_or_close(db)
    receipt = (metadata or {}).get("delivery_id")
    if receipt is not None and (not isinstance(receipt, str) or not 1 <= len(receipt) <= 180):
        raise Problem(422, "Invalid Klaudimero delivery receipt")
    delivery = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["klaudimero", profile, receipt]))) if receipt else str(uuid.uuid4())
    store = Store()
    digest = hashlib.sha256(content.encode()).hexdigest()
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT digest FROM channel_messages WHERE id=?", (delivery,)).fetchone()
        if row and row[0] != digest:
            raise Problem(409, "Klaudimero delivery receipt was already used for another message")
        if not row and conn.execute("SELECT count(*) FROM channel_messages WHERE status!='notified'").fetchone()[0] >= 2048:
            raise Problem(503, "Klaudimero delivery queue is full")
        conn.execute("INSERT OR IGNORE INTO channel_messages (id,profile,content,digest,status,created) VALUES (?,?,?,?,?,?)",
                     (delivery, profile, content, digest, "queued", time.time()))
    drain(store)
    # A live adapter owns a worker; standalone cron also leaves a durable queue
    # for the gateway/dashboard worker if a canonical transcript is busy.
    from .delivery import service
    service(str(store.directory))[1].start()
    return {"success": True, "message_id": delivery}


def drain(store):
    """Resume interrupted deliveries using a stable marker in Hermes's own DB.

    SessionDB's public append API enforces runtime/compression writer fences. A
    busy transcript stays queued; we never acquire or bypass another turn lease.
    The file lock serializes our independent gateway/dashboard/cron processes.
    """
    import fcntl
    with (store.directory / "channel-delivery.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        with store.connection() as conn:
            rows = conn.execute("""SELECT q.* FROM channel_messages q
                WHERE q.status!='notified' AND q.next_attempt<=? AND NOT EXISTS (
                    SELECT 1 FROM channel_messages earlier WHERE earlier.profile=q.profile
                        AND earlier.status!='notified' AND earlier.rowid<q.rowid)
                ORDER BY q.rowid LIMIT 16""", (time.time(),)).fetchall()
        for row in rows:
            from hermes_state_registry import release_or_close
            db = None
            try:
                profile, home = profile_target("bot:" + row["profile"])
                db = history(home)
                sid, lineage = canonical(db)
                marker = "klaudimero:" + row["id"]
                if row["status"] == "queued":
                    if not any(db.has_platform_message_id(part, marker) for part in lineage):
                        holder = "klaudimero-delivery:" + row["id"]
                        if not db.try_acquire_session_turn_lease(sid, holder, ttl_seconds=60):
                            raise Problem(409, "Main chat is busy; delivery remains queued")
                        try:
                            db.append_message(sid, "assistant", row["content"], platform_message_id=marker,
                                              message_uid=row["id"], timestamp=row["created"], turn_lease_holder=holder)
                        finally:
                            db.release_session_turn_lease(sid, holder)
                    # If the process died after append, the marker above prevents
                    # another transcript row on recovery, including compression.
                    from .notification_preview import preview
                    notification = preview(row["content"])
                    with store.connection() as conn:
                        conn.execute("UPDATE channel_messages SET status='written',session_id=?,content='',notification_preview=? WHERE id=?", (sid, json.dumps(notification), row["id"]))
                else:
                    sid = row["session_id"]
                    lineage = db.get_compression_lineage(sid)
                    notification = json.loads(row["notification_preview"]) if row["notification_preview"] else None
                store.enqueue(profile, sid, "channel:" + row["id"], "reply_ready", session_aliases=lineage, notification=notification)
                with store.connection() as conn:
                    conn.execute("UPDATE channel_messages SET status='notified',error=NULL,notification_preview=NULL WHERE id=?", (row["id"],))
            except Exception as error:
                # Queue text and delivery metadata stay in private storage. No
                # content, tokens, profile names or raw exceptions are logged.
                code = error.code if isinstance(error, Problem) else "Main chat is busy or temporarily unavailable"
                with store.connection() as conn:
                    conn.execute("UPDATE channel_messages SET attempts=attempts+1,next_attempt=?,error=? WHERE id=?",
                                 (time.time() + min(300, 5 * 2 ** min(row["attempts"], 6)), code, row["id"]))
            finally:
                if db is not None:
                    release_or_close(db)


async def standalone_send(config, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
    if thread_id or media_files or force_document:
        return {"error": "Klaudimero currently delivers text to the bot's main chat; attachments and threads are unsupported"}
    try:
        return await asyncio.to_thread(submit, chat_id, message)
    except Problem as error:
        return {"error": error.code}
    except Exception:
        return {"error": "Klaudimero could not persist the delivery"}


def list_targets():
    from hermes_cli.profiles import list_profile_names
    return [{"id": "bot:" + p, "name": "Bot Chat · " + p, "type": "dm"}
            for p in dict.fromkeys(["default", *list_profile_names()])]


def register(ctx):
    def adapter(config):
        from .channel_adapter import KlaudimeroAdapter
        return KlaudimeroAdapter(config)
    ctx.register_platform(
        name="klaudimero", label="Klaudimero", adapter_factory=adapter, check_fn=lambda: True,
        validate_config=lambda config: True, is_connected=lambda config: True,
        required_env=[], emoji="🐣", max_message_length=0,
        env_enablement_fn=lambda: {"home_channel": {"chat_id": "bot", "name": "Bot Chat"}},
        cron_deliver_env_var="KLAUDIMERO_HOME_CHANNEL",
        standalone_sender_fn=standalone_send,
        dashboard_description="Deliver job results and proactive messages to Klaudimero bot chats. Encrypted push is enrolled in the app; no credentials to enter here.",
        dashboard_required_env=(), docs_url="https://github.com/hartlco/klaudimero-hermes-plugin#delivery-channel",
    )
