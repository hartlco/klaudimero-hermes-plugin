# Version 0.4.0 verification

43 Python tests pass on Python3.14 against real Hermes v0.22.5 (`bf0203a794a07aac3ff764972ab9087baa4e4f4e`). Runtime-backed Plugin Doctor reports no findings: discovery, manifest, import and registration succeed with network blocked during registration. No model tools or prompts are added.

Channel tests use the actual PlatformEntry, gateway adapter, dashboard channel catalog, gateway config loader, cron delivery targets and standalone send dispatcher. Both live gateway and standalone cron sends append to real SessionDB main chats, including named profiles and compression tips. Public turn leases defer busy chats; FIFO retries and process-death marker recovery avoid overtaking and duplicate transcript rows. Unknown/archived chats and unsupported threads/attachments fail honestly. Explicit channel disable is preserved. Actual HPKE outbox packets contain no delivery text and honor profile/session enrollment, including compression ancestors. Named-profile pin replacement requires an explicit update flag and a fixed public repository.

The existing33 icon/enrollment/encryption/relay tests still pass. This release changes the Hermes plugin and app installer; relay0.3.0 stays compatible and does not need redeployment. Native app verification is recorded with the private app release. Tests use temporary homes, profiles, database files and synthetic keys; no production transcripts or Apple key are touched. Physical-device Apple delivery is not claimed by this test suite. GitHub Actions remains absent.

---

# Version 0.3.0 verification

33 Python tests pass, including device ownership proof, rejected wrong-owner/proof/signature requests, recipient/host scoping, revocation and expired grants, no resurrection of revoked UUIDs, token rotation and stable delivery deduplication, retry after uncertain APNs challenge dispatch, body limits and registration throttling. Managed plugin API tests verify that Hermes stores a grant and content public key without an APNs token/environment, retires replacement grants safely and refuses unverified grants. Existing icon and HPKE tests remain covered.

Runtime-backed Plugin Doctor against Hermes v0.22.5 passes discovery, manifest parsing, import and registration: zero tools and one lifecycle hook. Registration is network free.

The native app opens a Python-generated ownership challenge with CryptoKit, verifies Python and native HTTP signatures against the same bytes, and rejects substituted devices, service URLs, context, ciphertext and expired challenges. Three focused iPhone simulator UI tests passed automatic enrollment, revocation/fresh re-enrollment, forged-challenge rejection and service-unavailable recovery. The fixture runs the real relay/router/signature code and injects encrypted packets through simctl; it does not prove physical-device Apple delivery.

No real Apple key was used. Server DNS/TLS/key deployment, actual APNs acceptance and an independent security audit are not claimed. GitHub Actions remains absent. Published public image `ghcr.io/hartlco/klaudimero-push-relay:0.3.0` (also `latest`) from `8c24d01882b6b4f26080147aea3d19a50fe1302f`, OCI index `sha256:8cb20157f0ae3654cf742e2e3188721fc4f3576b5648a0b5fb9a853e1ef3aac9`. Anonymous pulls passed for both AMD64 and ARM64. Both actual containers passed signed device proof/enrollment, host grant validation, encrypted forwarding through a simulated APNs provider, duplicate delivery, revocation and rejection of revoked grant reuse. Both also passed production Compose startup with a synthetic mounted Apple key, UID10001/read-only root/capability checks, plaintext rejection, HTTP health, and persistent delivery deduplication after restart. Legacy bearer authentication was configured only inside its compatibility test; managed tests used no shared credential. [Receipts](verification/docker-0.3.0) include both runtime architectures and installed packages.

---

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
