# Klaudimero companion for Hermes

Optional server support for the Klaudimero iPhone, iPad and native Mac app:

- Sync editable chick avatars, symbol choices and imported bot photos between devices signed in to the same Hermes account.
- Register device tokens and enrolled chats for reply-ready/error notifications.
- Send generic notification events through a separately operated APNs relay. **The Hermes plugin never needs an Apple `.p8` key.**

The app continues to chat without this plugin. The plugin registers no model tools, injects no prompts and never patches Hermes core. The code targets the documented plugin hook and authenticated dashboard-router surfaces of **Hermes v0.22.5**.

## Install

In Klaudimero, open **More → Settings → Klaudimero plugin → Install plugin**. Review the source and confirm. The app uses Hermes's own authenticated plugin installer with an exact reviewed Git commit; it never runs an agent prompt to install code. The plugin's setup endpoint uses the public Hermes CLI to enable the same package for named profiles. It never forces an overwrite of modified packages or restarts a running gateway automatically.

Manual installation:

```sh
hermes plugins install https://github.com/hartlco/klaudimero-hermes-plugin --enable
# For each existing named bot that should emit notifications:
hermes -p my-bot plugins install https://github.com/hartlco/klaudimero-hermes-plugin --enable
```

Restart the dashboard if its plugin API is not available. Restart the relevant gateways after installing/enabling notification hooks if your Hermes deployment did not activate them live. Icon sync works as soon as the API is mounted. The app does not restart services during an active conversation.

## Where data lives

Hermes mounts `dashboard/plugin_api.py` under `/api/plugins/klaudimero` behind its usual authentication middleware. Durable data is outside the plugin checkout, in the default Hermes home's `plugin-data/klaudimero/companion.sqlite3`. A private operator override is available through `KLAUDIMERO_DATA_DIR`.

Icon records are partitioned by a digest of Hermes's verified dashboard identity and by profile ID. A local-token-only dashboard uses the digest of its host-validated owner token. There is no body parameter that lets a client select another account. Keep the same sign-in method/account on devices that should share settings. Unrecognized identity contexts fail closed; the plugin is not an unauthenticated standalone API.

The database holds full editable icon settings, optional photo bytes, revision numbers, mutation receipts, device tokens, enrolled profile/session IDs, and a generic notification outbox. It contains no conversation text or dashboard passwords. Data is protected by filesystem permissions, not separately encrypted against the Hermes OS user. Keep it with normal private Hermes backups. Updating plugin code leaves it intact.

## Icon synchronization

Writes carry `expected_revision` and a UUID `request_id`. SQLite commits the icon and retry receipt in one transaction. Replaying the identical request returns its original result; changing a request under an existing UUID is refused. Concurrent/stale edits receive `409 avatar_revision_conflict`; the app keeps the local choice and offers **Use server icon** or **Keep this device's icon**. It never silently overwrites a newer remote edit.

On initial migration, an existing server icon wins over an unmodified old device-only preference. Otherwise, the local preference is uploaded with expected revision zero. Explicit new edits, offline changes and in-flight saves remain protected. The lightweight capabilities response includes an aggregate icon revision, so unchanged photos are not downloaded every polling interval.

## Notifications

1. Enable notifications in the app and approve Apple's permission prompt.
2. The app obtains a fresh APNs token and sends it to its authenticated Hermes connection with an opaque per-connection device ID and enrolled profile/session IDs. Device tokens are not cached on disk by the app.
3. The plugin's documented `on_session_end` hook queues completed/failed turns for matching enrollments. Interrupted turns, subagent child turns and legacy exit payloads without a stable turn ID stay quiet.
4. A leased SQLite outbox sends events to the relay. Multiple gateway workers can share it. Stable event IDs deduplicate repeated hooks and settled relay requests. Transient failures retry with backoff; an invalid device is removed.
5. The relay signs an APNs request. The alert says only **Your agent has a new reply** or **Your agent needs your attention**. Routing metadata opens the already saved server/bot/chat; pushes cannot supply a server URL or credentials.

Klaudimero suppresses a foreground banner when that chat is already visible. Turning a device's notifications off removes its registration and unsent events; an offline removal is retried when the app reconnects. Enrollments expire 30 days after the last successful renewal. Only enrolled chats notify; the app retains previous enrollments through relaunch/token refresh and adds main Bot Chats and conversations it discovers/opens. This is not a broadcast of every account's sessions.

**Apple delivery is not configured by installation alone.** Configure the relay separately when its Apple key is available. The app reports missing relay setup and can still sync icons. See [relay deployment](docs/relay.md).

## Privacy boundaries

The relay receives a device token, APNs environment, opaque server/device/event IDs and profile/session routing IDs, plus a fixed event kind. It never receives icon settings, photo bytes, chat text, tool arguments, prompts, usernames or dashboard credentials. These generic alerts do not require decrypting message previews in a notification extension. No claim is made that routing metadata is hidden from the relay or Apple.

Every Hermes installation uses a distinct operator-issued relay credential; no shared sending secret is embedded in the public plugin or app. The relay rejects unauthenticated clients, arbitrary alert text/topics and reused event IDs with different payloads. Its APNs key remains a private deployment secret. TLS is required between the plugin and relay, with redirects disabled.

## Develop and verify

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q --import-mode=importlib
hermes plugins doctor . --ci
```

Tests cover account isolation, complete icon/photo/symbol persistence, compare-and-swap races and receipt retries, device token rotation/revocation, scoped enrollments, deduplicated hooks, authenticated relay delivery and synthetic-key ES256/APNs packet construction. No real Apple key or phone is needed for those tests. Hardware delivery requires the separately configured relay and a push-entitled app build. No GitHub Actions workflow is included.

