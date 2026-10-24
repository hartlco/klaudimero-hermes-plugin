# Companion 0.6.0

The release retains the reconciled proactivity channel and authenticated encrypted reply previews. It adds account-scoped Mac activity leases so a phone does not alert while the user is working in Klaudimero on a Mac.

The activity endpoint requires the normal authenticated Hermes identity and validates a UUID, boolean state and increasing safe integer sequence. Server time controls the 45-second lease. Replayed heartbeats cannot extend it, older requests cannot overwrite a newer inactive state, multiple Macs retain independent leases, and no Apple permission/registration is required to report Mac activity.

Mobile events suppressed while the Mac is active become stable outbox tombstones, including pending retries. Leaving the Mac does not replay a burst of old notifications. New events resume after deactivation or expiry. macOS registrations remain eligible and other authenticated owners are unaffected. Legacy device rows migrate to platform `unknown`, treated as mobile for compatibility. Message contents and previews remain encrypted before reaching the existing relay; no relay deployment/certificate change is required.

Validation: 74 integration cases passed against each actual Hermes v0.22.4 and v0.22.5 checkout before the final migration test was added. All 7 activity tests, including that additional real SQLite/API migration check, then passed. The real on_session_end callback reads a named profile's completed reply and produces a signed HPKE packet containing that answer, excluding tool/reasoning text. No production Apple key, device token or account was used.
