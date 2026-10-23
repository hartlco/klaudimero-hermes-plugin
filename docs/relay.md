# Relay deployment (Apple credentials can be added later)

The relay is a separate service, not code that places an Apple key on each Hermes host:

```text
Klaudimero ── authenticated dashboard ── Hermes plugin
                                            │ HTTPS, installation credential
                                            ▼
                                      Push relay ── APNs ── iPhone / Mac
                                      private .p8 key
```

BigHelp's managed setup similarly keeps its provider sending credentials in its notification service rather than on the Hermes host: [its primary notification documentation](https://github.com/promptclickrun/bighelp-plugin/blob/main/docs/NOTIFICATIONS.md). It additionally ships a legacy direct APNs provider. This implementation uses its own encrypted-payload relay contract; it does not call BigHelp/Cadu services or reuse their keys. Cadu's public plugin key handling was not independently verified.

## Configure the relay when ready

Create an APNs signing key for the Apple team that signs Klaudimero. Keep its `.p8` outside this Git checkout. The relay needs:

- `APNS_KEY_FILE`: path to its mounted private key.
- `APNS_KEY_ID` and `APNS_TEAM_ID`: Apple's key/team identifiers.
- `APNS_TOPIC`: `co.hartl.HermesApple` for the distributed app. A differently signed fork uses its own bundle ID and compatible key.
- `KLAUDIMERO_RELAY_CLIENTS`: a private JSON map of installation names to unique random credentials of at least 32 characters. Generate credentials with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`; do not put the actual result in Git or release notes.
- `KLAUDIMERO_RELAY_DATA`: durable private data directory (default `/data`). The delivery database holds hashes/status/leases for deduplication, not notification text or device tokens.

The service can start without Apple credentials and honestly reports `apns_ready: false`; push requests then return `503 apns_not_configured`. It never simulates success. Both sandbox and production APNs are supported per registered token. An environment-restricted Apple key must cover that environment; a rejected token/key cannot be fixed by changing client version headers.

Run Python directly:

```sh
uvicorn relay.app:create_app --factory --host 127.0.0.1 --port 8081 --no-access-log
```

Or, once the secret file and environment exist:

```sh
sudo docker compose -f relay/compose.yaml up -d --build
```

The container runs as UID 10001. Make the mounted secret readable by that service user and keep the data volume private. Place HTTPS in front of the loopback port, for example using Caddy.

### Klaudimero deployment

The selected relay URL is `https://klaudimero-push.hartl.co`. The operator supplies the server, DNS, TLS proxy and private Apple key; selecting this hostname does not deploy the service.

Copy `relay/.env.example` to a private path on the server, such as `/opt/klaudimero-push/relay.env`, and restrict it to the operator (`chmod 600`). Set `APNS_KEY_PATH` to the actual `.p8` path, fill in its key ID and set `KLAUDIMERO_RELAY_CLIENTS` to a JSON map containing a freshly generated installation credential. Do not put a Developer ID certificate or Mac signing private key on the relay: it needs only the APNs key. For a new production-only APNs key, select the topic `co.hartl.HermesApple`; that key will not send sandbox pushes from development builds.

From this repository's root on the server:

```sh
sudo docker compose --env-file /opt/klaudimero-push/relay.env -f relay/compose.yaml config --quiet
sudo docker compose --env-file /opt/klaudimero-push/relay.env -f relay/compose.yaml up -d --build
```

The proxy should forward to `127.0.0.1:8081`. If Caddy runs directly on the host, its site block is:

```caddyfile
klaudimero-push.hartl.co {
    reverse_proxy 127.0.0.1:8081
}
```

Point the hostname's DNS to that host and make HTTPS available before connecting Hermes. An authenticated `GET /v1/status` must report `apns_ready: true`; that confirms the relay found its configuration, not that Apple has accepted a push. Validate delivery on a physical device after installing the app and registering for notifications.

## Configure each Hermes host

Set these in the environment of the Hermes gateway/dashboard processes (including plugin hosts), then restart the relevant processes:

```text
KLAUDIMERO_PUSH_RELAY_URL=https://klaudimero-push.hartl.co
KLAUDIMERO_PUSH_RELAY_TOKEN=<this Hermes installation's relay credential>
```

The credential belongs to this relay installation. **It is not an Apple key**, and it is never returned by plugin capabilities or installed by the app. Use a separate credential per host so an operator can revoke one installation without exposing another's credential. The relay has no anonymous registration or unauthenticated sending endpoint.

## Delivery semantics and operations

The relay provides `/v1/status` and `/v1/push`, both requiring the host credential. It accepts only a v2 opaque encrypted envelope, APNs token/environment and stable event ID, caps a host at 120 new deliveries/minute, and persists a claim before calling Apple. Repeated settled events are no-ops. Reusing an ID with different data is rejected. Unknown network outcomes retain a lease before retry; the same APNs ID/collapse ID is reused, but this does not promise exactly-once display on a phone. Invalid tokens return 410 for plugin removal.

No plaintext event kind, chat routing or chat content passes through this service. The relay has no decryption or Hermes signing keys. The APNs fallback is generic; local notification service extensions decrypt and authenticate the event. See [the encrypted format](encrypted-push.md). Apple accepts only tokens for the app's signed team/topic/environment; a key for another app cannot deliver to Klaudimero. Validate a real physical-device push after configuring the production relay. Synthetic-key tests verify packet signing and routing, not Apple's acceptance of a real deployment.

