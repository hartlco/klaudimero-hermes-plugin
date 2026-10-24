"""Small, user-visible reply previews. Never include reasoning or tool payloads."""
import unicodedata


def bounded(value, maximum):
    value = ''.join(c if not unicodedata.category(c).startswith('C') else ' ' if c.isspace() else '' for c in value)
    value = ' '.join(value.split())
    raw = value.encode('utf-8')
    if len(raw) <= maximum:
        return value
    return raw[:maximum-3].decode('utf-8', errors='ignore').rstrip() + '…'


def preview(text, title='Klaudimero'):
    if not isinstance(text, str):
        return None
    body = bounded(text, 512)
    return {'title': bounded(title, 96), 'body': body} if body else None


def session_preview(profile, session, kind):
    if kind != 'reply_ready':
        return None
    db = None
    try:
        from hermes_cli.profiles import get_profile_dir, list_profile_names
        from .channel import history
        if profile not in {'default', *list_profile_names()}:
            return None
        db = history(get_profile_dir(profile))
        for message in reversed(db.get_messages(session, limit=32, latest=True)):
            # A newer user row means this is no longer the completed turn's tail.
            if message.get('role') == 'user':
                break
            if message.get('role') != 'assistant' or message.get('tool_calls'):
                continue
            if message.get('display_kind') not in (None, '', 'text', 'message'):
                continue
            text = message.get('content')
            if isinstance(text, list):
                text = '\n'.join(p.get('text', '') for p in text if isinstance(p, dict) and p.get('type') == 'text')
            if isinstance(text, str) and not text.lstrip().startswith(('[IMPORTANT: Background process', '[INTERNAL NOTIFICATION')):
                result = preview(text)
                if result:
                    return result
        return None
    except Exception:
        # Missing/unreadable history must never suppress the encrypted event or
        # cause another profile/turn's text to become a substitute preview.
        return None
    finally:
        if db is not None:
            from hermes_state_registry import release_or_close
            release_or_close(db)
