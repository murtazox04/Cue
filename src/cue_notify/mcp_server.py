"""Model Context Protocol server: lets AI agents notify people through Cue.

The server is a thin client of a running Cue API, authenticated with an ordinary API
key — so agents get exactly the scopes you grant them and every send goes through the
same rules, policy and audit trail as your own code::

    cuectl mcp --url https://cue.example.com --api-key ck_…   # stdio (Claude, Cursor…)

Requires ``pip install 'cue-notify[mcp]'``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from cue_notify import __version__
from cue_notify.core.time import utcnow

INSTRUCTIONS = """\
Cue decides and prepares notifications for people identified by an external id and
hands them to the configured delivery routes (push, SMS, e-mail, webhooks…).

Prefer `send_event` to report what happened and let the configured rules decide what
to send; use `send_message` for an explicit one-off message
with a known template. Always pass an idempotency key derived from the triggering
action so retries never notify someone twice. Messages may be suppressed by the
recipient's preferences, frequency caps or quiet hours — check `status` and
`status_reason`, and use `explain` to see why a rule did or did not fire.

People's attention is limited and shared with every other sender. Send only what the
person would want now, pick `importance` honestly and set an expiry on anything that
goes stale. If your key belongs to an agent, you also have a per-person budget
(`suppressed` with reason `agent_budget` means you used it up) and some messages wait
for a human (`pending_approval`): do not retry those, they are not lost.

Before an action with consequences a person should own (deleting data, spending money,
contacting many people), call `request_approval` and wait: check `get_approval` until its
`status` is `approved`, `rejected` or `expired`, and treat anything but `approved` as no.
"""

type F = Callable[..., Any]

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
SENDS = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)


def _result(response: httpx.Response) -> Any:
    if response.is_success:
        return response.json() if response.content else {"ok": True}
    try:
        problem = response.json()
        detail = problem.get("detail") or problem.get("title")
        if problem.get("errors"):
            detail = f"{detail}: {problem['errors']}"
    except ValueError:
        detail = response.text[:300]
    raise ToolError(f"Cue returned {response.status_code}: {detail}")


def _id(value: str) -> str:
    """A path segment from a model-supplied id. Ids are UUIDs; anything else (such as
    ``../recipients/…``) is refused rather than resolved into another endpoint."""
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ToolError(f"{value!r} is not a valid id") from exc


def _tool(server: MCPServer, annotations: ToolAnnotations) -> Callable[[F], F]:
    """Register a tool with its docstring, dedented, as the description agents read."""

    def register(fn: F) -> F:
        server.tool(description=inspect.cleandoc(fn.__doc__ or ""), annotations=annotations)(fn)
        return fn

    return register


def create_server(client: httpx.AsyncClient) -> MCPServer:
    """Build the MCP server around an HTTP client pointed at the Cue API."""
    server = MCPServer(
        name="cue",
        title="Cue notifications",
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://github.com/murtazox04/Cue",
    )

    @_tool(server, SENDS)
    async def send_event(  # pyright: ignore[reportUnusedFunction]
        name: str,
        recipient: str,
        data: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Report that something happened to a recipient (e.g. "order.shipped").

        Matching rules decide whether and how to notify them. `recipient` is the
        person's external id; `data` is available to rule conditions and templates.
        """
        body = {"name": name, "recipient": recipient, "data": data or {}}
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        return _result(await client.post("/v1/events", json=body))

    @_tool(server, SENDS)
    async def send_message(  # pyright: ignore[reportUnusedFunction]
        recipient: str,
        template: str,
        channels: list[str],
        data: dict[str, Any] | None = None,
        category: str = "transactional",
        importance: Literal["low", "normal", "high", "critical"] = "normal",
        expires_in_minutes: int | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Send a template directly to one recipient over the given channels (fallback order).

        Choose `importance` honestly: `low` never interrupts, `high` is time-sensitive and
        `critical` (security or safety only) may break through quiet hours. Set
        `expires_in_minutes` when the message is useless later (codes, arrivals).
        Returns the message, whose `status` may be `queued` or `suppressed`.
        """
        body: dict[str, Any] = {
            "recipient": recipient,
            "template": template,
            "channels": channels,
            "data": data or {},
            "category": category,
            "importance": importance,
        }
        if expires_in_minutes is not None:
            body["expires_at"] = (utcnow() + timedelta(minutes=expires_in_minutes)).isoformat()
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        return _result(await client.post("/v1/messages", json=body))

    @_tool(server, READ_ONLY)
    async def get_message(message_id: str) -> Any:  # pyright: ignore[reportUnusedFunction]
        """Delivery status, channel, content and engagement of one message."""
        return _result(await client.get(f"/v1/messages/{_id(message_id)}"))

    @_tool(server, READ_ONLY)
    async def list_messages(  # pyright: ignore[reportUnusedFunction]
        recipient: str | None = None,
        status: Literal[
            "queued",
            "sending",
            "sent",
            "delivered",
            "failed",
            "suppressed",
            "cancelled",
            "pending_approval",
        ]
        | None = None,
        limit: int = 20,
    ) -> Any:
        """Recent messages, newest first, optionally for one recipient or status."""
        params: dict[str, Any] = {"limit": max(1, min(limit, 100))}
        if recipient:
            params["recipient"] = recipient
        if status:
            params["status"] = status
        return _result(await client.get("/v1/messages", params=params))

    @_tool(server, SENDS)
    async def request_approval(  # pyright: ignore[reportUnusedFunction]
        title: str,
        reviewers: list[str],
        body: str = "",
        importance: Literal["low", "normal", "high", "critical"] = "high",
        expires_in_minutes: int = 60,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Ask people (`reviewers`, by external id) to approve or reject an action before you
        take it. Each reviewer is notified with a one-time link. Returns the request;
        poll `get_approval` with its `id`. Undecided requests expire: treat that as no."""
        payload = {
            "title": title,
            "body": body,
            "reviewers": reviewers,
            "importance": importance,
            "expires_in_seconds": max(60, expires_in_minutes * 60),
            "metadata": metadata or {},
        }
        return _result(await client.post("/v1/approvals", json=payload))

    @_tool(server, READ_ONLY)
    async def get_approval(approval_id: str) -> Any:  # pyright: ignore[reportUnusedFunction]
        """An approval request: `status` is `pending`, `approved`, `rejected` or `expired`,
        with the reviewer's `comment`."""
        return _result(await client.get(f"/v1/approvals/{_id(approval_id)}"))

    @_tool(server, READ_ONLY)
    async def get_event(event_id: str) -> Any:  # pyright: ignore[reportUnusedFunction]
        """An event and, per rule, whether it matched and what happened."""
        return _result(await client.get(f"/v1/events/{_id(event_id)}"))

    @_tool(server, READ_ONLY)
    async def explain(  # pyright: ignore[reportUnusedFunction]
        event_name: str, recipient: str, data: dict[str, Any] | None = None
    ) -> Any:
        """Dry run: which rules would fire for this event and recipient, and why not.

        Nothing is stored or sent. Requires an admin key.
        """
        body = {"name": event_name, "recipient": recipient, "data": data or {}}
        return {"rules": _result(await client.post("/v1/rules/evaluate", json=body))}

    @_tool(server, READ_ONLY)
    async def preview_template(  # pyright: ignore[reportUnusedFunction]
        template: str, data: dict[str, Any] | None = None, locale: str | None = None
    ) -> Any:
        """Render a template with sample data for every channel. Requires an admin key."""
        body = {"data": data or {}, "locale": locale}
        return _result(
            await client.post(f"/v1/templates/{quote(template, safe='')}/preview", json=body)
        )

    @_tool(server, READ_ONLY)
    async def list_templates() -> Any:  # pyright: ignore[reportUnusedFunction]
        """Available templates and their locales. Requires an admin key."""
        page = _result(await client.get("/v1/templates", params={"limit": 200}))
        return {
            "templates": [
                {
                    "key": item["key"],
                    "description": item["description"],
                    "locales": list(item["locales"]),
                }
                for item in page["items"]
            ]
        }

    return server


def run(
    url: str,
    api_key: str,
    transport: Literal["stdio", "streamable-http"] = "stdio",
    **options: Any,
) -> None:
    """Run the server until the client disconnects (stdio) or the process is stopped."""
    client = httpx.AsyncClient(
        base_url=url.rstrip("/"),
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": f"cue-mcp/{__version__}"},
        timeout=30.0,
    )
    create_server(client).run(transport, **options)
