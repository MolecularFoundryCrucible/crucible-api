"""A limit on request body size, checked before anything reads the body: the declared
Content-Length first, then the bytes as they arrive, for bodies sent without one. Oversized requests
get 413 (RFC 9110 15.5.14). Thumbnails, sent as base64 JSON, get a larger limit."""
import json
import re

from app.config import get_settings

THUMBNAIL_WRITE = re.compile(r"/datasets/[^/]+/thumbnails(/[^/]+)?/?$")


class _TooLarge(Exception):
    pass


class BodySizeLimit:
    def __init__(self, app):
        self.app = app

    @staticmethod
    def limit_for(method: str, path: str) -> int:
        api = get_settings().api
        if method in ("POST", "PATCH", "PUT") and THUMBNAIL_WRITE.search(path):
            return api.max_thumbnail_body_mib * 1024 * 1024
        return api.max_body_mib * 1024 * 1024

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.limit_for(scope["method"], scope["path"])
        declared = next((value for name, value in scope["headers"] if name == b"content-length"), None)
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            return await _refuse(send, limit)
        started = False
        received = 0

        async def counted_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge
            return message

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except _TooLarge:
            if started:
                raise
            await _refuse(send, limit)


async def _refuse(send, limit: int) -> None:
    body = json.dumps({"detail": {"code": "body_too_large", "message": f"The request body is larger than the {limit // (1024 * 1024)} MiB this route accepts", "limit_bytes": limit}}).encode()
    await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})
