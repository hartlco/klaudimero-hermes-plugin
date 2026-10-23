"""Bound JSON input before parsing, including requests without Content-Length."""
from fastapi.responses import JSONResponse


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        parts, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            size += len(body)
            if size > 8192:
                return await JSONResponse({"detail": "request_too_large"}, status_code=413)(scope, receive, send)
            parts.append(body)
            if not message.get("more_body", False):
                break
        buffered = b"".join(parts)
        replayed = False
        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": buffered, "more_body": False}
            return await receive()
        async def private_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [(b"cache-control", b"no-store")]
            await send(message)
        await self.app(scope, replay, private_send)
