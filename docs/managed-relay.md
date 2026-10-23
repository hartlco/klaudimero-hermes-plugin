# Managed device grants v2

The app and plugin pin `https://klaudimero-push.hartl.co`. Capabilities advertise `push_relay: {protocol: 2, url: "https://klaudimero-push.hartl.co"}`. The app accepts only this reviewed service, follows no redirects and never sends Apple device tokens through Hermes. No shared credential ships with either component.

## Request authentication

Requests carry `X-Klaudimero-Key` (standard Base64 raw Ed25519 public key), `X-Klaudimero-Time` (integer Unix seconds), `X-Klaudimero-Request` (UUID) and `X-Klaudimero-Signature` (standard Base64 signature). Sign these exact UTF-8 bytes, without a trailing newline:

```text
Klaudimero relay request v1\nMETHOD\n/path\ntimestamp\nrequest-uuid\nhex-sha256(raw-body)
```

Methods are uppercase, generated UUIDs lowercase. Requests are accepted within 300 seconds of server time. No query parameters are used. Raw HTTP bodies are capped at 8192 bytes before parsing. Request UUIDs bind the signature; mutation grant/challenge IDs and delivery event IDs provide durable operation identity. A fresh signature alone does not bypass those constraints.

## Device ownership

The app creates a global Ed25519 signing key and X25519 agreement key in its app-only, device-only Keychain. Per-connection content keys remain separate.

1. `POST /v2/devices/challenges` signed by that device identity, with `{challenge_id, device_token, environment, encryption_key}`. Keys use standard Base64 raw 32-byte public keys. Tokens use lowercase hex, environment is `production` or `sandbox`.
2. The service generates a random 32-byte proof (64 lowercase hex characters), stores only its hash, and sends its encrypted packet solely through APNs. The HTTP result is only `{challenge_id, verified: false}`. The same challenge ID is dispatched at most once, including uncertain provider responses. Already verified owner/token/environment pairs renew with `verified: true` and no extra notification.
3. The foreground app accepts a challenge only if its ID is currently pending for this token. It decrypts and validates owner, expiry and authenticated context, then signs `POST /v2/devices/challenges/{id}/confirm` with `{proof}`. The service checks owner/proof/expiry, consumes that owner's outstanding challenges, and records the verified token for 45 days. The public API never returns a proof or packet.

APNs carries the generic setup alert and `klaudimero_relay_enrollment` with `{version: 1, challenge_id, owner_id, ephemeral_key, salt, nonce, ciphertext}`. `owner_id = hex SHA256(raw Ed25519 public key)`. Encryption uses a fresh ephemeral X25519 key, a random 32-byte salt and 12-byte nonce. Derive AES-256 key with HKDF-SHA256(shared X25519 secret, salt, info=context). Both HKDF info and AES-GCM authenticated data are:

```text
UTF8("Klaudimero relay enrollment v1\0" + lowercase_challenge_uuid + "\0" + owner_id)
```

Plaintext is compact JSON `{proof, expires_at}` with a five-minute expiry. Ciphertext includes the 16-byte GCM tag. All binary fields are standard Base64. The app suppresses the setup notification while processing it; no content decryption identity or Apple private key is involved. Backgrounding the app can interrupt initial verification; it reports a timeout and allows retry.

Challenge creation is limited globally to 100/minute, per peer to 20/minute and per token/environment to 5/5 minutes. At most 3000 live challenges exist. Proof possession, rather than merely knowing a token or minting a signing key, is required to register a host.

## Grants, delivery and revocation

- The verified app signs `POST /v2/grants` with `{grant_id, host_key, recipient_key_id}`. `host_key` is Hermes's pinned Ed25519 public key; `recipient_key_id` is SHA-256 of this connection's content public key. The returned record contains only these fields and `expires_at`. Up to 32 active grants per device; at most 100 new grants per day. Grants renew for 30 days only under the same device identity and exact scope.
- The app sends only `{relay_grant_id, encryption: {version: 2, public_key}, sessions}` to the authenticated plugin's existing device registration route. The plugin signs `GET /v2/grants/{id}` with its private host key and checks all returned scope fields before storing it. No APNs token or environment is stored in a managed Hermes registration.
- Hermes signs `POST /v2/push` with `{grant_id, event_id, encrypted}`. The service requires the matching host, recipient-key ID and active grant/device before resolving the APNs destination internally. The [existing signed HPKE content envelope](encrypted-push.md) stays encrypted from Hermes to the notification extension.
- The app or host may sign `DELETE /v2/grants/{id}`. Unknown grants are idempotent; a foreign identity cannot revoke an existing one. Revoked IDs retain tombstones and cannot be recreated, even by replaying a previously valid creation request. A new enrollment uses a new UUID.

The app persists pending grant cleanup and removes its local content key immediately on opt-out. The plugin stops its local registration/outbox and persists a second revocation queue; either side can complete cleanup after reconnect. An offline relay cannot instantly revoke a previously accepted Apple delivery, but the removed content key prevents local decryption/routing. Token rotations require a new ownership proof and do not change existing event deduplication identity.
