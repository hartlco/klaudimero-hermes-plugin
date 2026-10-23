"""Outbound-only relay transport and a leased, retryable notification sender."""
import json
import logging
import os
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

log = logging.getLogger("klaudimero")


class ManagedRelay:
    def __init__(self, store):
        from .relay_auth import RELAY_URL
        self.store, self.url = store, RELAY_URL
        self.opener = build_opener(NoRedirect)

    def request(self, method, path, value=None):
        from .relay_auth import sign_request
        from .contracts import Problem
        body = json.dumps(value, separators=(",", ":")).encode() if value is not None else b""
        headers = sign_request(self.store.push_signing_key(), method, path, body)
        headers["Content-Type"] = "application/json"
        request = Request(self.url + path, method=method, data=body if body else None, headers=headers)
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(8193)
                if len(raw) > 8192:
                    raise ValueError()
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError()
                return value
        except HTTPError as error:
            raise Problem(error.code, "notification_grant_inactive" if error.code == 410 else "notification_service_unavailable") from None
        except (URLError, OSError, TimeoutError, ValueError):
            raise Problem(503, "notification_service_unavailable") from None

    def verify_grant(self, grant_id, recipient_id):
        from .contracts import Problem
        value = self.request("GET", "/v2/grants/" + grant_id)
        if (value.get("grant_id") != grant_id or value.get("host_key") != self.store.push_signing_public_key
                or value.get("recipient_key_id") != recipient_id or type(value.get("expires_at")) is not int
                or not time.time() < value["expires_at"] <= time.time() + 31 * 86400):
            raise Problem(403, "notification_grant_mismatch")
        return value

    def revoke(self, grant_id):
        from .contracts import Problem
        if self.request("DELETE", "/v2/grants/" + grant_id).get("revoked") is not True:
            raise Problem(503, "notification_service_unavailable")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Relay:
    def __init__(self, url=None, token=None, store=None):
        self.url = (url if url is not None else os.environ.get("KLAUDIMERO_PUSH_RELAY_URL", "")).rstrip("/")
        self.token = token if token is not None else os.environ.get("KLAUDIMERO_PUSH_RELAY_TOKEN", "")
        parts = urlsplit(self.url)
        self.legacy_configured = bool(self.token and parts.scheme == "https" and parts.hostname and not parts.username and not parts.query and not parts.fragment)
        self.managed = ManagedRelay(store) if store else None
        self.configured = self.legacy_configured or self.managed is not None

    def send(self, payload):
        if payload.get("grant_id") and self.managed:
            from .contracts import Problem
            try:
                value = self.managed.request("POST", "/v2/push", payload)
                return "sent" if value.get("accepted") is True else "retry"
            except Problem as error:
                return "invalid_device" if error.status == 410 else "retry"
        if not self.legacy_configured:
            return "retry"
        request = Request(self.url + "/v1/push", method="POST", data=json.dumps(payload).encode(), headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with build_opener(NoRedirect).open(request, timeout=15) as response:
                return "sent" if response.status == 200 else "retry"
        except HTTPError as error:
            if error.code == 410:
                return "invalid_device"
            # Auth/configuration errors also retain the outbox for an operator fix.
            return "retry"
        except (URLError, OSError, TimeoutError):
            return "retry"


class Sender:
    def __init__(self, store, relay=None):
        self.store, self.relay = store, relay or Relay(store=store)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.thread = None

    def start(self):
        if not self.relay.configured:
            return
        with self.lock:
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, name="klaudimero-push", daemon=True)
                self.thread.start()
        self.wake.set()

    def run(self):
        while not self.stop.is_set():
            try:
                if getattr(self.relay, "managed", None):
                    for grant in self.store.pending_revocations():
                        try:
                            self.relay.managed.revoke(grant)
                            self.store.finish_revocation(grant)
                        except Exception:
                            # A failed old cleanup must not starve other devices.
                            log.warning("Klaudimero grant cleanup will retry")
                for _ in range(32):
                    if self.stop.is_set():
                        break
                    row = self.store.claim()
                    if not row:
                        break
                    outcome = self.relay.send(json.loads(row["payload"]))
                    self.store.settle(row["id"], retry=outcome == "retry", drop_device=outcome == "invalid_device")
            except Exception:
                # Do not log tokens, profile/session identifiers, or HTTP response bodies.
                log.warning("Klaudimero notification sender could not process its outbox")
            self.wake.wait(15)
            self.wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()

