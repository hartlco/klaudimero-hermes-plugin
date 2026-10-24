"""Gateway adapter; imported only when Hermes brings the channel online."""
import asyncio

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .channel import list_targets, submit
from .contracts import Problem


class KlaudimeroAdapter(BasePlatformAdapter):
    # Human input still enters through the authenticated Hermes session API.
    # No listener or alternative unauthenticated ingress is created here.
    def __init__(self, config):
        super().__init__(config, Platform("klaudimero"))
        self.sender = None

    async def connect(self, *, is_reconnect=False):
        from .delivery import Sender
        from .storage import Store
        self.sender = Sender(Store())
        self.sender.start()
        self._mark_connected()
        return True

    async def disconnect(self):
        if self.sender:
            self.sender.close()
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if reply_to or (metadata or {}).get("thread_id"):
            return SendResult(success=False, error="Klaudimero delivers to the main bot chat without threads")
        try:
            result = await asyncio.to_thread(submit, chat_id, content, metadata=metadata)
            return SendResult(success=True, message_id=result["message_id"])
        except Problem as error:
            return SendResult(success=False, error=error.code, retryable=error.status == 503)
        except Exception:
            return SendResult(success=False, error="Klaudimero could not persist the delivery", retryable=True)

    async def list_channels(self):
        return list_targets()

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "dm"}
