"""A cap on request bodies, enforced before anything parses them.

FastAPI reads and validates a JSON body before the API-key dependency runs, so without a
cap an unauthenticated client could make the server buffer and parse gigabytes. Bodies
over the limit get ``413`` whether they announce their size or stream it.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cue_notify.api.problems import MEDIA_TYPE
from cue_notify.core.enums import Scope as KeyScope
from cue_notify.runtime import Runtime
from cue_notify.services import api_keys

BROADCASTS = "/v1/broadcasts"


def app_path(scope: Scope) -> str:
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if not root_path or not path.startswith(root_path):
        return path
    rest = path[len(root_path) :]
    if not rest:
        return "/"
    return rest if rest.startswith("/") else path


def bearer_token(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == b"authorization":
            scheme, _, credentials = value.decode("latin-1").partition(" ")
            credentials = credentials.strip()
            if scheme.lower() == "bearer" and credentials:
                return credentials
            return None
    return None


async def holds_admin_key(scope: Scope) -> bool:
    token = bearer_token(scope)
    if token is None:
        return False
    runtime: Runtime = scope["app"].state.runtime
    async with runtime.transaction() as session:
        key = await api_keys.authenticate(session, token)
        return key is not None and api_keys.allows(key, KeyScope.ADMIN)


class BodyLimit:
    def __init__(self, app: ASGIApp, *, default: int, broadcasts: int) -> None:
        self.app = app
        self.default = default
        self.broadcasts = broadcasts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = app_path(scope)
        limit = self.default
        # Audiences of up to a million external ids are legitimate there (admin only).
        if path.rstrip("/") == BROADCASTS and await holds_admin_key(scope):
            limit = self.broadcasts
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
