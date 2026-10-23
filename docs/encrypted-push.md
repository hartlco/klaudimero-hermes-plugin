# Encrypted push v2

The recipient public key is raw 32-byte X25519, standard Base64 in authenticated device enrollment: `encryption: {version: 2, public_key: "..."}`. The recipient ID is lowercase SHA-256(raw public key). The capabilities response identifies `hpke-x25519-sha256-aes128gcm-ed25519-v2` and a standard-Base64 raw 32-byte Ed25519 signing public key. Its private counterpart stays in the private Hermes SQLite metadata; back it up with the database.

Hermes creates UTF-8 JSON containing `destination` (the v1 typed server/device/profile/session/event/kind object), `issued_at` and `expires_at` Unix seconds. Lifetime is at most 3600 seconds. The signing input is:

```text
UTF8("Klaudimero encrypted push v2\0" + key_id + "\0" + event_id) || payload_bytes
```

It signs those exact bytes with Ed25519. Signed plaintext is JSON `{payload: Base64(payload_bytes), signature: Base64(signature)}`. A fresh RFC 9180 base-mode HPKE context uses DHKEM(X25519, HKDF-SHA256), HKDF-SHA256 and AES-128-GCM, suite IDs 0x0020/0x0001/0x0001. `info` is the domain/context above; AEAD additional data is empty. Single-shot output is 32-byte encapsulated key followed by ciphertext/tag. Ed25519 authenticates Hermes, which HPKE base mode alone does not do.

The external envelope is `{version: 2, key_id, event_id, ciphertext: Base64(enc || ct)}`. Managed `/v2/push` carries only `{grant_id, event_id, encrypted: envelope}` under the host HTTP signature. The service resolves the token/environment from its device-authorized grant. Legacy `/v1/push` accepts `{event_id, device_token, environment, encrypted: envelope}` only with an explicitly configured private operator credential. APNs receives fixed generic alert text, sound, `mutable-content: 1`, and `klaudimero_encrypted: envelope`. There is no plaintext routing fallback. Opaque IDs, token, app topic, size and timing remain observable delivery metadata.

The device selects its Keychain identity by key ID, opens one fresh CryptoKit HPKE recipient, verifies the Ed25519 signature/context, parses payload, checks lifetime/future skew (300 seconds), and binds destination server/device/event IDs. The extension changes only local presentation; it preserves the original encrypted userInfo. The app repeats verification when receiving/tapping it. Plaintext routing dictionaries, a substituted signing key, unknown recipient, modified ciphertext/context and expired events fail closed. Before first unlock or when keys are missing, the extension returns only the generic fallback. This is freshness validation and delivery deduplication, not an exactly-once or ratcheting messaging protocol.

The generic APNs alert is necessarily visible to the push service to invoke Apple's notification service extension. Its text contains no event kind or chat details. The meaningful event and routing data are encrypted from Hermes to the device. Initial key exchange uses the authenticated Hermes dashboard connection and needs HTTPS against active network substitution. Explicit re-pairing is required for a legitimate changed server identity.

Python cryptography's RFC 9180 implementation seals; Apple CryptoKit opens. Cross-language synthetic-key vectors and tamper/identity/expiry checks live in the app tests. No custom Diffie-Hellman, HKDF or AEAD implementation is used.
