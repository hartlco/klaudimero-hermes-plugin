# Version 0.2.0 verification

The plugin is checked against real Hermes v0.22.5 (`bf0203a794a07aac3ff764972ab9087baa4e4f4e`). Runtime-backed Plugin Doctor passes discovery, import and registration with network blocked during registration: zero tools and one lifecycle hook. The declared bounded cryptography dependency is installed in the test runtime.

25 Python tests pass on Python 3.14.7. They cover account isolation, avatar CAS/receipts, concurrent writers, complete icon records, encryption-only registration, malformed/low-order X25519 public keys, HPKE round trips, sender authentication, wrong recipients/signers, context/ciphertext tampering, encrypted outbox retry byte stability, legacy plaintext retirement, token/encryption-key rotation, relay v1 rejection, authenticated relay deduplication and synthetic ES256/APNs packet construction. The Apple packet stays below its payload limit and contains only ciphertext plus a generic fallback.

Apple CryptoKit opens Python HPKE packets in the private app's synthetic interop vector tests, including expiry and identity checks. Native extension/Keychain and presentation/routing evidence is kept with that app. Tests use synthetic keys. The owner has since created/downloaded production APNs key `29QRZSQJXQ`, but it was not used in these tests. Production relay deployment, physical-device APNs acceptance and an independent cryptographic audit are not claimed. GitHub Actions is disabled.

## Public Docker image: 2026-10-23

Published `ghcr.io/hartlco/klaudimero-push-relay:0.2.0` and `:latest` from source commit `da9a513124a5abf57121e4f7463b8d796ba2c889` (source tag `relay-v0.2.0`). Both resolve to OCI index `sha256:088a32871f2a4ee27dd00e07f2e350d963cee67896081f554d6b5dc0470b4e7a` with Linux `amd64` and `arm64` manifests. The image is public: anonymous registry access and Docker pulls using an empty authentication configuration passed for both platforms. Build/publish ran locally; no Actions workflow was added or used.

The published images were started through the production Compose configuration, changing only the test image/platform and temporary port. AMD64 ran under emulation on the ARM64 release Mac. Both passed:

- Non-root UID `10001`, read-only root filesystem and healthy HTTP service.
- Unauthenticated requests rejected with `401`; authenticated status found the mounted synthetic Apple key.
- Plaintext notification fields rejected with `422`.
- A settled synthetic delivery stayed deduplicated across a container restart using the persistent volume. This test never sent to Apple.
- Synthetic ES256 provider token signing, verification and token caching inside each image.

The container test found and fixed the original short-form secret mount naming mismatch: the key is now explicitly mounted as `/run/secrets/apns.p8`, matching the provider configuration. The documented environment generator was executed from its Markdown block: matching credentials, mode `0600`, overwrite protection and Compose validation passed. Disposable test containers and volumes were removed.

The [Compose file](../relay/compose.yaml) pulls the published image directly and explicitly selects `linux/amd64`; no source checkout/build is needed on the server. [Machine-readable receipts](verification/docker-0.2.0) record registry digests, installed packages and both runtime results. Actual server startup, DNS/TLS, the operator's private key mount and real phone delivery remain deployment steps.
