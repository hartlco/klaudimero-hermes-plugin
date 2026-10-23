# Klaudimero companion for Hermes

Optional server support for the Klaudimero iPhone, iPad and native Mac app:

- Sync editable chick avatars, symbol choices and imported bot photos between devices signed in to the same Hermes account.
- Register device tokens and enrolled chats for reply-ready/error notifications.
- Encrypt and authenticate notification events for each device before forwarding through a separately operated APNs relay. **The Hermes plugin never needs an Apple `.p8` key.**

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

## Encrypted notifications

1. Enable notifications and approve Apple's native permission prompt. The app creates an X25519 key pair in its device-only shared Keychain. Only the public key is enrolled with the authenticated Hermes account. Device APNs tokens remain memory-only in the app.
2. The authenticated capabilities response supplies Hermes's stable Ed25519 public identity. The app pins it with the server/account/device identity in Keychain. An unexpected identity change stops enrollment; explicitly disable/re-enable notifications after independently checking a legitimate server key replacement.
3. Documented `on_session_end` hooks queue completed/failed turns for matching enrolled chats. Hermes signs the event and its routing context, then encrypts it with **RFC 9180 HPKE: X25519 / HKDF-SHA256 / AES-128-GCM**. Each event uses a fresh ephemeral key. The signed plaintext is never sent to the relay.
4. The durable SQLite outbox stores the sealed packet once. Retries send identical ciphertext/event IDs; key or token rotation retires obsolete pending packets. Legacy devices without an encryption key and queued plaintext events never fall back to plaintext delivery.
5. The relay accepts only the encrypted v2 contract. It forwards ciphertext plus a generic **Open Klaudimero to view an encrypted notification** fallback and `mutable-content: 1` to APNs. It has no device private keys or Hermes signing keys.
6. The bundled notification service extension decrypts locally and verifies the pinned sender, event context, expiry, server and device before displaying the reply/error copy. The app independently verifies the original sealed packet before opening a saved bot/chat. Invalid, expired, unpaired and plaintext v1 packets never navigate. If the extension cannot access its key or times out, only the generic fallback appears.

Opt-out removes the local private identity immediately, then removes the server registration/outbox, retrying remote cleanup after reconnect. Removing/replacing a saved connection first revokes its registration. Enrollments expire after 30 days without renewal. Only chats discovered/opened/enrolled by the app notify. Imported icons and chat content do not pass through this relay.

**Apple delivery is not configured by installation alone.** Configure the relay separately when its Apple key is available. The app reports missing relay setup and can still sync icons. The prebuilt `ghcr.io/hartlco/klaudimero-push-relay:0.2.0` image supports Linux AMD64/ARM64, with a [copy-and-paste Docker setup](docs/relay.md#docker-quick-start-amd64-server) and [ready-made Compose file](relay/compose.yaml). See [the encryption wire format](docs/encrypted-push.md).

## Privacy and trust boundaries

The relay sees a device token, APNs environment, opaque recipient-key/event IDs, ciphertext size and delivery timing. Apple additionally knows the app topic and generic fallback. **Server/device routing IDs, profile/session IDs and event kind are inside the encrypted payload.** No icon settings, photos, prompts, usernames, passwords or plaintext conversation content are accepted by the relay.

Enrollment and first signing-key pinning use the app's existing authenticated Hermes connection. **Use HTTPS for an authenticated initial key exchange and protection against an active network attacker.** Encryption does not repair an untrusted or intercepted bootstrap connection. Once pinned, a replacement signing key is refused until explicitly re-paired. Private device keys are non-synchronizing Keychain items protected after the device's first unlock, shared only with this app's signed notification extension. They are never uploaded or stored in preferences.

Every Hermes installation has a distinct operator-issued relay credential. The `.p8` key stays in the relay's runtime secret mount. Ciphertext is authenticated and fresh for one hour; stable event IDs and delivery leases reduce duplicates, without promising exactly-once Apple display or a ratcheting/forward-secure conversation protocol. This release encrypts fixed completion/failure events and routing, not rich chat previews.

## Upgrading an existing pinned installation

Version 0.2.0 requires encrypted registration and a matching updated relay. Upgrade the app, relay and every profile's plugin, then restart the dashboard/gateways so an old worker cannot continue emitting v1 events. Existing v1.1 app installs are pinned; Hermes deliberately refuses an ordinary update of a pinned checkout. After reviewing and backing up any local code changes, move that pin with Hermes's documented command:

```sh
hermes plugins install https://github.com/hartlco/klaudimero-hermes-plugin --force --ref <reviewed-0.2.0-commit> --enable
hermes -p my-bot plugins install https://github.com/hartlco/klaudimero-hermes-plugin --force --ref <reviewed-0.2.0-commit> --enable
```

The app never forces that overwrite or bypasses Hermes's scan/capability decisions. Icon/device data is outside the checkout. New installations use the app's pinned installer. Hermes's package manager must admit the declared `cryptography>=50,<51` dependency; resolve any reported dependency conflict through Hermes rather than forcing incompatible core packages.

## Develop and verify

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q --import-mode=importlib
hermes plugins doctor . --ci
```

Tests cover account isolation, complete icon/photo/symbol persistence, compare-and-swap races and receipt retries, device token rotation/revocation, scoped enrollments, deduplicated hooks, authenticated relay delivery and synthetic-key ES256/APNs packet construction. No real Apple key or phone is needed for those tests. Hardware delivery requires the separately configured relay, push-entitled app, correctly signed notification extension and matching shared Keychain entitlement. No GitHub Actions workflow is included.

