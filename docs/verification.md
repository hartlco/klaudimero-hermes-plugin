# Version 0.2.0 verification

The plugin is checked against real Hermes v0.22.5 (`bf0203a794a07aac3ff764972ab9087baa4e4f4e`). Runtime-backed Plugin Doctor passes discovery, import and registration with network blocked during registration: zero tools and one lifecycle hook. The declared bounded cryptography dependency is installed in the test runtime.

25 Python tests pass on Python 3.14.7. They cover account isolation, avatar CAS/receipts, concurrent writers, complete icon records, encryption-only registration, malformed/low-order X25519 public keys, HPKE round trips, sender authentication, wrong recipients/signers, context/ciphertext tampering, encrypted outbox retry byte stability, legacy plaintext retirement, token/encryption-key rotation, relay v1 rejection, authenticated relay deduplication and synthetic ES256/APNs packet construction. The Apple packet stays below its payload limit and contains only ciphertext plus a generic fallback.

Apple CryptoKit opens Python HPKE packets in the private app's synthetic interop vector tests, including expiry and identity checks. Native extension/Keychain and presentation/routing evidence is kept with that app. No production `.p8` key, relay deployment, physical-device APNs acceptance or independent cryptographic audit is claimed. Apple key setup remains deferred by the owner. GitHub Actions is disabled.
