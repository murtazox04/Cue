"""A cap on request bodies, enforced before anything parses them.

FastAPI reads and validates a JSON body before the API-key dependency runs, so without a
cap an unauthenticated client could make the server buffer and parse gigabytes. Bodies
over the limit get ``413`` whether they announce their size or stream it.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cue_notify.api.problems import MEDIA_TYPE

BROADCASTS = "/v1/broadcasts"


class BodyLimit:
    def __init__(self, app: ASGIApp, *, default: int, broadcasts: int) -> None:
        self.app = app
        self.default = default
        self.broadcasts = broadcasts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"][len(scope.get("root_path", "")) :] or scope["path"]
        # Audiences of up to a million external ids are legitimate there (admin only).
        limit = self.broadcasts if path.rstrip("/") == BROADCASTS else self.default
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > limit):
            await _reject(send, limit)
            return

        received = 0
        started = False
        rejected = False

        async def limited_receive() -> Message:
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Answer now, then tell the app the client went away.
                    rejected = True
                    if not started:
                        await _reject(send, limit)
                    return {"type": "http.disconnect"}
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if rejected:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        await self.app(scope, limited_receive, tracking_send)


async def _reject(send: Send, limit: int) -> None:
    body = (
        b'{"type":"about:blank","title":"Content too large","status":413,'
        b'"detail":"request bodies are limited to ' + str(limit).encode() + b' bytes"}'
    )
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", MEDIA_TYPE.encode()),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
