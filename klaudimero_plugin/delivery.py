"""Outbound-only relay transport and a leased, retryable notification sender."""
import json
import logging
import os
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

log = logging.getLogger("klaudimero")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Relay:
    def __init__(self, url=None, token=None):
        self.url = (url if url is not None else os.environ.get("KLAUDIMERO_PUSH_RELAY_URL", "")).rstrip("/")
        self.token = token if token is not None else os.environ.get("KLAUDIMERO_PUSH_RELAY_TOKEN", "")
        parts = urlsplit(self.url)
        self.configured = bool(self.token and parts.scheme == "https" and parts.hostname and not parts.username and not parts.query and not parts.fragment)

    def send(self, payload):
        if not self.configured:
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
        self.store, self.relay = store, relay or Relay()
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

