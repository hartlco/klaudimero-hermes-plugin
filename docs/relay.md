# Relay deployment

**For app users:** install the plugin and enable notifications in Klaudimero. No relay URL, shared credential or Hermes environment file is needed. The app authorizes a host-scoped grant automatically after proving device ownership through encrypted APNs. See the [protocol](managed-relay.md).

**For the relay operator:** deploy this service once with the app's Apple key. The `.p8` belongs only on this service, outside Git. It is never installed on users' Hermes hosts. This service does not use BigHelp/Cadu infrastructure or keys.

## Docker quick start: AMD64 server

The public image `ghcr.io/hartlco/klaudimero-push-relay:0.3.0` supports Linux AMD64/ARM64. Use the [filled deployment file](../relay/compose.hartl.yaml) for `klaudimero-push.hartl.co`. It contains the public key/team/topic identifiers, requires no `.env`, and expects `AuthKey_29QRZSQJXQ.p8` beside the Compose file:

```sh
mkdir -p klaudimero-push
cd klaudimero-push
curl -fL https://raw.githubusercontent.com/hartlco/klaudimero-hermes-plugin/relay-v0.3.0/relay/compose.hartl.yaml -o compose.yaml
# Put AuthKey_29QRZSQJXQ.p8 in this directory yourself.
sudo chown 10001:10001 AuthKey_29QRZSQJXQ.p8
sudo chmod 0400 AuthKey_29QRZSQJXQ.p8
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose up -d
sudo docker compose ps
```

Compose file-backed secrets are bind mounts on Linux; the source key must be readable by service UID `10001`. The image runs without root, with a read-only root filesystem, dropped capabilities and a private persistent SQLite volume. It contains no Apple key or shared production password. Do not remove the volume on upgrade: it stores verified devices, grants, revocations and delivery receipts.

Point DNS for `klaudimero-push.hartl.co` to this server. A Caddy proxy on the host can use:

```caddyfile
klaudimero-push.hartl.co {
    reverse_proxy 127.0.0.1:8081
}
```

A proxy in another container must share the relay's Docker network and use `klaudimero-push:8080`; its own loopback is not the host. Expose only the HTTPS proxy. Configure its request-body limit/rate limiting as appropriate for the deployment and retain the relay's own signature, payload and enrollment limits. Preserve the original client address through a trusted proxy for per-peer throttling. Uvicorn trusts forwarding headers only from configured trusted proxy peers; never trust arbitrary public forwarding headers.

The production-only key `29QRZSQJXQ` is restricted to `co.hartl.HermesApple`; it cannot send sandbox notifications from development builds. Release-testing apps exported for production APNs and the notarized Mac release use production tokens. Actual Apple acceptance must be checked on a physical device after DNS, HTTPS and the private key mount are working. A Docker health check confirms the HTTP service is alive, not successful APNs delivery.

There is **nothing to add to Hermes's Docker Compose or systemd environment**. Install/update the plugin from Klaudimero and enable notifications. Leave the app open during the first encrypted verification. Each device grants/revokes access independently; no user account at the relay or shared setup secret is required.

## Other operators and compatibility

The [generic Compose file](../relay/compose.yaml) accepts `APNS_KEY_PATH`, `APNS_KEY_ID` and `APNS_TEAM_ID` from the operator's environment; `APNS_KEY_FILE` points to the mounted secret and `APNS_TOPIC` is the app bundle ID. `KLAUDIMERO_RELAY_DATA` optionally changes the private database path (default `/data`). Forks using another relay must change the reviewed relay URL in both app and plugin and sign their app with a compatible Apple topic. Servers cannot silently redirect an app's device enrollment to an arbitrary URL.

Legacy `/v1/status` and `/v1/push` remain available only if an operator explicitly supplies `KLAUDIMERO_RELAY_CLIENTS`, a private map of unique installation bearer credentials. It is optional and is not needed for managed `/v2` enrollment. Previously issued private credentials should be removed after old installations migrate. Never embed a common bearer token in the public plugin or app. Apple keys and Mac signing private keys do not belong on Hermes.

## Delivery semantics and privacy

Managed `/v2` requests require Ed25519 signatures over the exact method, path, timestamp, request UUID and body hash. Device ownership is proved with a random five-minute challenge sent only via APNs, encrypted to a device agreement key. Grant creation requires that proof. Sending requires the designated host's signature, an active grant and matching content-key ID. Grant revocation is idempotent and cannot be undone by retrying an old creation request.

The relay stores device tokens and their environment to resolve opaque grant IDs, plus public signing keys, content-key hashes, lease/revocation state and delivery receipts. It does not store device private keys, icons, chat text, plaintext routing or dashboard credentials. Keep its database and backups private. Verified device records expire after 45 days without renewal, grants after 30; expired device/challenge cleanup occurs during enrollment. Revocation tombstones remain to reject replay of old grant IDs.

The outbox and relay deduplicate stable events. Unknown Apple outcomes keep a delivery lease before retrying, using the same APNs ID/collapse ID; exactly-once phone display is not promised. An accepted push may already be queued at Apple when a grant is revoked. Invalid tokens/grants retire the matching registration without deleting a replacement enrollment. Encrypted content and routing are fresh for one hour. The service forwards a generic fallback; the app's signed notification extension decrypts and authenticates locally.
