# Version 0.1.1 verification

The plugin is checked against the real **Hermes v0.22.5**, source commit `bf0203a794a07aac3ff764972ab9087baa4e4f4e`. Hermes's runtime-backed Plugin Doctor reports successful manifest discovery/import/registration, **zero tools and one on_session_end hook**, with network access blocked during registration. The test interpreter is Python 3.14.7.

The plugin/relay test suite covers authenticated identity partitioning, complete chick/symbol/photo records, stale-state rejection, identical retry receipts, concurrent writers, token rotation, lease/claim exclusion, revocation, duplicate hook events, quiet interrupted/subagent turns, metadata-only change detection, relay authentication and idempotency, missing Apple credentials, invalid-device handling, and ES256/APNs payload construction using an ephemeral synthetic P-256 key. No real Apple key is checked into the repository or used by the tests.

These checks do not claim physical-device APNs acceptance or an already deployed hosted relay. Apple credentials and the actual relay hostname are intentionally configured later. The app integration and simulator evidence live in the private Klaudimero app repository. GitHub Actions is not used.

