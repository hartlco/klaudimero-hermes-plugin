# Klaudimero companion for Hermes

Optional server support for the Klaudimero iPhone, iPad and native Mac app:

- Sync editable chick avatars, symbol choices and imported bot photos between devices signed in to the same Hermes account.
- Deliver scheduled job results and proactive messages into each bot’s main chat through the **Klaudimero** Hermes channel.
- Register device-scoped notification grants and enrolled chats for reply-ready/error notifications.
- Encrypt and authenticate notification events for each device before forwarding through a separately operated APNs relay. **The Hermes plugin never needs an Apple `.p8` key.**

The app continues to chat without this plugin. The plugin registers a local delivery channel and notification hooks, adds no model tools or prompts, and never patches Hermes core. The code targets the documented plugin hook and authenticated dashboard-router surfaces of **Hermes v0.22.5**.

## Install

In Klaudimero, open **More → Settings → Klaudimero plugin → Install plugin**. Review the source and confirm. Fresh installation uses Hermes's authenticated installer and an exact reviewed Git commit. Plugin 0.4.1 adds an authenticated updater that calls Hermes's public CLI with `--force --yes-deps` only after the app explicitly approves package replacement and the declared Python dependency. It verifies the installed Git SHA and stores an owner-scoped retry receipt. Fresh installation never overwrites an existing package. Icon/notification data stays outside the checkout; gateways are never restarted automatically. No model prompt is used to install code.

Manual installation:

```sh
hermes plugins install https://github.com/hartlco/klaudimero-hermes-plugin --enable
# For each existing named bot that should emit notifications:
hermes -p my-bot plugins install https://github.com/hartlco/klaudimero-hermes-plugin --enable
```

Restart the dashboard if its plugin API is not available. Restart the relevant gateways after installing/enabling notification hooks if your Hermes deployment did not activate them live. Icon sync works as soon as the API is mounted. Restarts are explicit actions in the app, with confirmation that active work can be interrupted.

Plugin **0.4.2** adds **Restart dashboard** for Linux systemd deployments. It walks from the isolated plugin host to its Hermes dashboard/serve ancestor, reads the unified cgroup and verifies that the unit's live MainPID is that exact dashboard process. It never accepts a unit, PID or command from the client, restarts a parent container/cron/user manager, escalates with sudo, or kills an unmanaged process. The systemd job is queued with `--no-block`; the app reports success only after a different dashboard process identity reconnects under the same server/account and loads the installed version. Receipts and an expected instance prevent repeated or stale requests. Permission refusals stay visible. Manual/Docker/launchd/wrapped services get a host-side restart instruction instead.

The app also offers **Restart gateway** through Hermes's existing authenticated action route. It checks the matching action PID, exit status and gateway readiness. This targets the default gateway; restart independently managed named-profile gateways on their host. Existing 0.4.1 routers need one host-side dashboard restart after updating to 0.4.2 before the new dashboard restart route becomes available. No restart runs as a side effect of installing/updating.

## Where data lives

Hermes mounts `dashboard/plugin_api.py` under `/api/plugins/klaudimero` behind its usual authentication middleware. Durable data is outside the plugin checkout, in the default Hermes home's `plugin-data/klaudimero/companion.sqlite3`. A private operator override is available through `KLAUDIMERO_DATA_DIR`.

Icon records are partitioned by a digest of Hermes's verified dashboard identity and by profile ID. A local-token-only dashboard uses the digest of its host-validated owner token. There is no body parameter that lets a client select another account. Keep the same sign-in method/account on devices that should share settings. Unrecognized identity contexts fail closed; the plugin is not an unauthenticated standalone API.

The database holds full editable icon settings, optional photo bytes, revision numbers, mutation receipts, notification grants, enrolled profile/session IDs, a generic notification outbox, and pending channel delivery text. Queued text is cleared after it is saved to the native Hermes transcript. No dashboard passwords are stored. Data is protected by filesystem permissions, not separately encrypted against the Hermes OS user. Keep it with normal private Hermes backups. Updating plugin code leaves it intact.

## Icon synchronization

Writes carry `expected_revision` and a UUID `request_id`. SQLite commits the icon and retry receipt in one transaction. Replaying the identical request returns its original result; changing a request under an existing UUID is refused. Concurrent/stale edits receive `409 avatar_revision_conflict`; the app keeps the local choice and offers **Use server icon** or **Keep this device's icon**. It never silently overwrites a newer remote edit.

On initial migration, an existing server icon wins over an unmodified old device-only preference. Otherwise, the local preference is uploaded with expected revision zero. Explicit new edits, offline changes and in-flight saves remain protected. The lightweight capabilities response includes an aggregate icon revision, so unchanged photos are not downloaded every polling interval.

## Delivery channel

Plugin **0.4.0** registers **Klaudimero** in Hermes’s web **Channels** page and scheduled-job delivery picker. The channel becomes available when this updated plugin is loaded in the dashboard and the relevant profile gateways. Existing 0.3.0 installations must update the package and restart those processes; DNS and relay settings do not control channel discovery.

No channel token, URL or Apple credential is required. The channel’s default destination is `bot`, meaning the running job/agent’s own profile. Explicit targets are `bot:default` or `bot:<profile-id>`; the gateway’s channel directory lists these bot targets. Choose **Klaudimero** as a job’s delivery channel, or use `send_message` with platform `klaudimero` and target `bot:<profile-id>` for proactive text. Open the target bot in Klaudimero once so its canonical **Bot Chat** exists. Missing, unknown and archived main chats return an error rather than claiming successful delivery.

Live gateway delivery and standalone cron delivery both persist the text in the existing main chat as an assistant message. The app’s normal history refresh displays it without restarting. This does not run another model turn. The plugin uses Hermes’s shared SessionDB registry and public append/turn-lease APIs; it never writes Hermes SQL or takes over another active turn. Busy deliveries remain durably queued and preserve per-bot order. A worker resumes them after the current turn finishes; a stable platform message marker prevents another transcript row if the worker dies after append but before recording completion. This guarantees recovery of an accepted queue item, not exactly-once delivery across independent caller invocations without a stable delivery ID.

The existing device enrollments receive an encrypted reply-ready event after the text is saved. Plaintext message content stays on Hermes; APNs receives only the existing encrypted generic event. Push opt-out does not disable delivery into chat. Queue text is kept privately until appended, then removed while its receipt/digest remains. Gateway workers and the dashboard/API sender resume pending work; an offline host resumes after it is running again. Keep the plugin data directory when updating.

Text deliveries are supported, up to 256 KiB. The adapter rejects thread-specific or attachment-only sends. The channel worker currently requires a Linux/macOS Hermes host. Enabling/disabling the channel through Hermes preserves the explicit disabled setting. Native chat input continues through Hermes’s existing authenticated session API; this plugin opens no public inbound listener.

## Encrypted notifications

1. Install the plugin from the app and enable **Notifications on this device**. Keep Klaudimero open during initial verification. No Hermes environment variables, relay token or relay URL are needed.
2. The app verifies ownership of its Apple device token directly with the Klaudimero service. A short-lived proof arrives only in an encrypted APNs challenge; the HTTP API never returns the proof. The app confirms it with a device-only Ed25519 Keychain identity. Knowing a token or generating a key does not authorize sending.
3. The app grants this Hermes server permission to notify this device, binding the grant to the server's Ed25519 identity and a per-connection X25519 content key. The app sends Hermes the grant ID, public content key and enrolled chats. Hermes never receives the managed device's APNs token, service identity private keys or Apple key.
4. Documented `on_session_end` hooks queue completed/failed turns for enrolled chats. Hermes signs the event and routing context, then seals it using **RFC 9180 HPKE: X25519 / HKDF-SHA256 / AES-128-GCM** with a fresh ephemeral key. The outbox persists sealed packets; retries retain their ciphertext/event IDs.
5. Hermes signs each HTTP request with its own private notification identity. The service verifies the host-scoped, recipient-bound grant before resolving its APNs token and forwarding ciphertext. The bundled notification extension decrypts locally, verifies the pinned Hermes identity, expiry, server and device, then displays the reply/error copy. The app independently verifies the original packet before opening a chat. Plaintext v1, altered, expired and unpaired packets never navigate.

Opt-out immediately removes local content keys, removes the Hermes registration/outbox, and revokes the service grant. Offline revocations are durably queued by both app and plugin. A new enrollment gets a fresh grant and content key; revoked grants cannot be resurrected. Grants expire after 30 days without renewal. Only chats discovered/opened/enrolled by the app notify.

The service operator deploys the Apple key once, separately from Hermes. Version **0.3.0** of the prebuilt image supports Linux AMD64/ARM64 and automatic device grants. See the [operator Docker setup](docs/relay.md#docker-quick-start-amd64-server), [filled Compose for klaudimero-push.hartl.co](relay/compose.hartl.yaml), [enrollment protocol](docs/managed-relay.md) and [notification encryption](docs/encrypted-push.md). Existing v0.2 relay deployments must upgrade their image before automatic enrollment will work. The app reports service or verification failures instead of claiming delivery is configured.

## Privacy and trust boundaries

The relay sees a device token, APNs environment, opaque recipient-key/event IDs, ciphertext size and delivery timing. Apple additionally knows the app topic and generic fallback. **Server/device routing IDs, profile/session IDs and event kind are inside the encrypted payload.** No icon settings, photos, prompts, usernames, passwords or plaintext conversation content are accepted by the relay.

Enrollment and first signing-key pinning use the app's existing authenticated Hermes connection. **Use HTTPS for an authenticated initial key exchange and protection against an active network attacker.** Encryption does not repair an untrusted or intercepted bootstrap connection. Once pinned, a replacement signing key is refused until explicitly re-paired. Private device keys are non-synchronizing Keychain items protected after first unlock. Only per-connection content keys are shared with the signed notification extension; the global enrollment identity stays in the app-only Keychain. They are never uploaded or stored in preferences.

Managed sending uses device-issued grants and the Hermes host signing key. No shared relay password is distributed to apps or plugins. The `.p8` key stays in the relay's runtime secret mount. Ciphertext is authenticated and fresh for one hour; stable event IDs and delivery leases reduce duplicates, without promising exactly-once Apple display or a ratcheting/forward-secure conversation protocol. This release encrypts fixed completion/failure events and routing, not rich chat previews.

## Upgrading an existing pinned installation

Plugin 0.4.1 fixes updating an active plugin with Python dependencies. App 0.2.4 uses the authenticated updater and retains failures across refreshes. Hermes 0.22.4/0.22.5's forced HTTP installer cannot carry dependency consent, so the old 0.2.3 app's Update action cannot upgrade 0.3/0.4. A restart alone does not fix that refusal. **Existing 0.3.0/0.4.0 installations need this one-time CLI upgrade**, including `--yes-deps`, on the default Hermes host. Restart its dashboard, install app 0.2.4, and use Update plugin to finish named bots. Subsequent updates work in the app.

The delivery channel remains compatible with relay 0.3.0; the relay needs no change. Existing icons, devices and chat history are retained.

Existing packages are pinned; Hermes deliberately refuses an ordinary update of a pinned checkout. **Update plugin** explicitly replaces the plugin code, including local edits; back those edits up first. It never bypasses Hermes's scan/capability decisions. Alternatively, move the pin using Hermes's documented CLI:

```sh
hermes -p default plugins install https://github.com/hartlco/klaudimero-hermes-plugin --force --ref e192e052e229c7c21699884b788939db64db4adb --enable --yes-deps
# Alternatively upgrade a named bot directly:
hermes -p my-bot plugins install https://github.com/hartlco/klaudimero-hermes-plugin --force --ref e192e052e229c7c21699884b788939db64db4adb --enable --yes-deps
```

Icon/device data is outside the checkout. New installations use the app's pinned installer. Hermes's package manager must admit the declared `cryptography>=50,<51` dependency; resolve any reported dependency conflict through Hermes rather than forcing incompatible core packages.

## Develop and verify

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q --import-mode=importlib
hermes plugins doctor . --ci
```

Tests cover account isolation, complete icon/photo/symbol persistence, compare-and-swap races and receipt retries, device proof, host-scoped grants, token rotation/revocation, scoped enrollments, deduplicated hooks, authenticated relay delivery and synthetic-key ES256/APNs packet construction. Set `HERMES_TEST_SOURCE` to a real supported Hermes checkout to also run channel integration tests. These exercise actual SessionDB guards, named profiles, compression, crash recovery, the platform adapter, dashboard catalog discovery, and cron delivery targets:

```sh
HERMES_TEST_SOURCE=/path/to/hermes-agent .venv/bin/pytest -q --import-mode=importlib
```

No real Apple key or phone is needed for those tests. Hardware delivery requires the separately configured relay, push-entitled app, correctly signed notification extension and matching shared Keychain entitlement. No GitHub Actions workflow is included.

