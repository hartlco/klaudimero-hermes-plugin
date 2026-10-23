"""Documented Hermes lifecycle hooks only: no core overrides or prompt changes."""
from .contracts import identifier, Problem
from .delivery import Sender
from .storage import Store


def normalize(profile, payload):
    if payload.get("interrupted") or payload.get("parent_session_id") or payload.get("platform") == "subagent":
        return None
    if payload.get("failed") is True:
        kind = "session_failed"
    elif payload.get("completed") is True:
        kind = "reply_ready"
    else:
        return None
    try:
        return (identifier(profile), identifier(payload.get("session_id")), identifier(payload.get("turn_id")), kind)
    except Problem:
        # Reduced legacy CLI exit payloads have no stable turn id; never guess it.
        return None


def register(ctx):
    sender = None

    def on_session_end(**payload):
        nonlocal sender
        event = normalize(ctx.profile_name, payload)
        if event is None:
            return
        if sender is None:
            sender = Sender(Store())
        sender.store.enqueue(*event)
        sender.start()

    def unload():
        if sender:
            sender.close()

    ctx.register_hook("on_session_end", on_session_end)
    ctx.on_unload(unload)

