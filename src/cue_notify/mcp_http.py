from __future__ import annotations

import hmac
import json

import httpx
from starlette.types import ASGIApp, Receive, Scope, Send

from cue_notify import __version__
from cue_notify.api.limits import bearer_token
from cue_notify.mcp_server import create_server

UNAUTHORIZED = json.dumps(
    {"error": "unauthorized", "error_description": "a valid bearer token is required"}
).encode()


class RequireBearer:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        presented = bearer_token(scope)
        if presented is not None and hmac.compare_digest(
            presented.encode("latin-1"), self.expected
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(UNAUTHORIZED)).encode()),
                    (b"www-authenticate", b'Bearer realm="cue-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": UNAUTHORIZED})


def create_app(url: str, api_key: str, *, host: str, token: str | None) -> ASGIApp:
    client = httpx.AsyncClient(
        base_url=url.rstrip("/"),
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": f"cue-mcp/{__version__}"},
        timeout=30.0,
    )
    app = create_server(client).streamable_http_app(host=host)
    return RequireBearer(app, token) if token else app


def serve(url: str, api_key: str, *, host: str, port: int, token: str | None) -> None:
    import uvicorn

    uvicorn.run(create_app(url, api_key, host=host, token=token), host=host, port=port)
