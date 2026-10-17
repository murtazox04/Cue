"""Model Context Protocol server: lets AI agents notify people through Cue.

The server is a thin client of a running Cue API, authenticated with an ordinary API
key — so agents get exactly the scopes you grant them and every send goes through the
same rules, policy and audit trail as your own code::

    cuectl mcp --url https://cue.example.com --api-key ck_…   # stdio (Claude, Cursor…)

Requires ``pip install 'cue-notify[mcp]'``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from cue_notify import __version__
from cue_notify.core.time import utcnow

INSTRUCTIONS = """\
Cue notifies people, identified by your system's id for them (their external id), over
the delivery routes configured on the server (push, SMS, e-mail, webhooks…), applying
their preferences and every sender's limits.

Pick the tool by intent:
- `send_event` reports what happened ("order.shipped") and lets the configured rules
  decide whether and what to send. Prefer it.
- `send_message` sends one known template directly, for an explicit one-off message.
- `request_approval` asks people before you take an action with consequences outside
  Cue (deleting data, spending money, contacting many people). Poll `get_approval` until
  `status` is `approved`, `rejected` or `expired`, and treat anything but `approved` as no.
- `get_event`, `get_message` and `list_messages` show what actually happened; `explain`
  dry-runs the rules for an event (admin keys only).

Always pass an idempotency key derived from the triggering action, so a retry never
notifies someone twice. People's attention is limited and shared with every other
sender: send only what the person would want now, pick `importance` honestly
(`critical` is for security and safety only) and set an expiry on anything that goes
stale.

Accepted is not delivered. Preferences, frequency caps and unsubscribes may suppress a
message and quiet hours may delay it: check `status` and `status_reason`. If your key
belongs to an agent, you also have a per-person budget (`suppressed` with reason
`agent_budget: …` means you used it up) and some messages wait for a person
(`pending_approval`): do not resend those, they are not lost.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
SENDS = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

ImportanceName = Literal["low", "normal", "high", "critical"]
MessageStatusName = Literal[
    "queued",
    "sending",
    "sent",
    "delivered",
    "failed",
    "suppressed",
    "cancelled",
    "pending_approval",
]

MESSAGE_FIELDS = (
    "id, status (queued, sending, pending_approval, sent, delivered, failed, suppressed or "
    "cancelled), status_reason (why it was suppressed, failed, held or cancelled, e.g. "
    "`frequency_cap: 3 per 1 day, 0:00:00` or `agent_budget: …`; null otherwise), "
    "category, template, channels (planned, in fallback order), channel (the one used), "
    "locale, content (rendered title, body, url… under `default` and each channel with "
    "its own override), importance, agent, event_id, rule_id, reviewed_by, reviewed_at, "
    "screening, attempts, send_at, expires_at, sent_at, delivered_at, opened_at, "
    "clicked_at, converted_at and created_at"
)


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


def create_server(client: httpx.AsyncClient) -> MCPServer:
    """Build the MCP server around an HTTP client pointed at the Cue API."""
    server = MCPServer(
        name="cue",
        title="Cue notifications",
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://github.com/murtazox04/Cue",
    )

    @server.tool(
        annotations=SENDS,
        description=(
            "Report that something happened to a person (e.g. `order.shipped`) and let "
            "Cue's rules decide whether and what to notify. Prefer it to send_message "
            "whenever a rule can cover the case. Needs the `events:write` scope.\n\n"
            "Rules run asynchronously, so the result only confirms receipt: `id`, `status` "
            "(`pending` until processed) and `duplicate` (true when this idempotency key "
            "was already reported; nothing new happens). Call get_event with the `id` to "
            "see, per rule, whether it matched and which message it produced. An unknown "
            "recipient gets an empty profile, so a message may end `suppressed` with "
            "`no_reachable_channel`. For agent keys, the messages count against the "
            "agent's per-person budget, are capped at its importance ceiling and may wait "
            "for approval. Fails with 403 for names starting with `cue.` and 422 for "
            "invalid input."
        ),
    )
    async def send_event(  # pyright: ignore[reportUnusedFunction]
        name: Annotated[
            str,
            Field(
                description="Event name, 1-255 letters, digits and `_ . : - /`, e.g. "
                "`order.shipped`. Rules listen for it by name or pattern (`order.*`). "
                "Names starting with `cue.` are reserved."
            ),
        ],
        recipient: Annotated[
            str,
            Field(
                description="External id of the person the event is about: the id your "
                "system uses for them, not a Cue UUID."
            ),
        ],
        data: Annotated[
            dict[str, Any] | None,
            Field(
                description="Event details as a JSON object, at most 16 KB. Rule "
                "conditions read it as `event.data`, templates as `data`. Defaults to {}."
            ),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(
                description="Up to 255 characters derived from the triggering action "
                "(e.g. `order-9-shipped`), unique per sender: a repeat is recognised and "
                "notifies nobody again. Strongly recommended."
            ),
        ] = None,
    ) -> Any:
        body = {"name": name, "recipient": recipient, "data": data or {}}
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        return _result(await client.post("/v1/events", json=body))

    @server.tool(
        annotations=SENDS,
        description=(
            "Send one template directly to one person over the channels you list, "
            "without rules. Use it for an explicit one-off message (a code, a reply, an "
            "alert) when you know the template key; otherwise prefer send_event. Needs "
            "the `messages:write` scope.\n\n"
            "The message still passes its category's policy (preferences, frequency caps, "
            "quiet hours, unsubscribes) and, for agent keys, the agent's per-person "
            "budget, importance ceiling and approval policy. Returns the message, with "
            "the same fields as get_message. `status` is `queued`, `suppressed` (read "
            "`status_reason`) or `pending_approval` (held for a person: do not resend). "
            "Repeating an `idempotency_key` returns the original message instead of "
            "sending again. Fails with 404 for an unknown template or category, 403 when "
            "an agent may not use the category and 422 for a channel the server does not "
            "have."
        ),
    )
    async def send_message(  # pyright: ignore[reportUnusedFunction]
        recipient: Annotated[
            str,
            Field(
                description="External id of the person: the id your system uses for "
                "them. An unknown id gets an empty profile without addresses, so the "
                "message is suppressed with `no_reachable_channel`."
            ),
        ],
        template: Annotated[
            str,
            Field(description="Key of an existing template, e.g. `order-shipped`."),
        ],
        channels: Annotated[
            list[str],
            Field(
                description="At least one channel name in fallback order, e.g. "
                '`["push", "email"]`; each must be configured on the server.'
            ),
        ],
        data: Annotated[
            dict[str, Any] | None,
            Field(
                description="Template variables as a JSON object, read as `data` in the "
                "template; at most 16 KB. Defaults to {}."
            ),
        ] = None,
        category: Annotated[
            str,
            Field(
                description="Policy category key. `transactional` (default) has no caps, "
                "ignores quiet hours and cannot be unsubscribed from; `marketing` is "
                "capped and respects quiet hours. Agent keys may use only their allowed "
                "categories."
            ),
        ] = "transactional",
        importance: Annotated[
            ImportanceName,
            Field(
                description="`low` never interrupts, `normal` is the default, `high` is "
                "time-sensitive, `critical` is for security or safety only and breaks "
                "through quiet hours and caps only in categories people cannot "
                "unsubscribe from (elsewhere it is sent as `high`). An agent's ceiling "
                "lowers anything above it."
            ),
        ] = "normal",
        expires_in_minutes: Annotated[
            int | None,
            Field(
                description="Drop the message if it is not delivered within this many "
                "minutes; set it for codes, arrivals and anything useless later. Omit "
                "for no expiry."
            ),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(
                description="Up to 200 characters derived from the triggering action; "
                "repeating it with the same API key returns the original message "
                "instead of sending again. Strongly recommended."
            ),
        ] = None,
    ) -> Any:
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

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Fetch one message by id to learn whether and how a person was notified: "
            "delivery status and its reason, the channel used, the rendered content and "
            "engagement. Use it after send_message, or with a `message_id` from "
            "get_event; use list_messages when you have no id. Needs the "
            "`messages:read` scope.\n\n"
            f"Returns {MESSAGE_FIELDS}. Fails with 404 when the id does not exist or, "
            "for an agent key, belongs to another sender."
        ),
    )
    async def get_message(  # pyright: ignore[reportUnusedFunction]
        message_id: Annotated[
            str,
            Field(
                description="Message UUID: the `id` returned by send_message or "
                "list_messages, or a `message_id` in get_event's `outcome`."
            ),
        ],
    ) -> Any:
        return _result(await client.get(f"/v1/messages/{_id(message_id)}"))

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "List recent messages, newest first, optionally for one person or status: "
            "what someone was already sent, what was suppressed, what waits for "
            "approval. Use get_message when you have an id. Needs the `messages:read` "
            "scope; agent keys see only their own messages.\n\n"
            "Returns `items`, each with the same fields as get_message, and "
            "`next_cursor`, which is not null when older messages exist beyond `limit`. "
            "Fails with 404 when `recipient` is not a known external id."
        ),
    )
    async def list_messages(  # pyright: ignore[reportUnusedFunction]
        recipient: Annotated[
            str | None,
            Field(description="External id of the person; omit for everyone."),
        ] = None,
        status: Annotated[
            MessageStatusName | None,
            Field(description="Only messages with this status; omit for any status."),
        ] = None,
        limit: Annotated[
            int,
            Field(description="Most messages to return, 1-100 (other values are clamped)."),
        ] = 20,
    ) -> Any:
        params: dict[str, Any] = {"limit": max(1, min(limit, 100))}
        if recipient:
            params["recipient"] = recipient
        if status:
            params["status"] = status
        return _result(await client.get("/v1/messages", params=params))

    @server.tool(
        annotations=SENDS,
        description=(
            "Ask people to approve or reject an action you are about to take outside Cue "
            "(deleting data, spending money, contacting many people), before you take "
            "it. Not for informing anyone: use send_event or send_message. Needs the "
            "`messages:write` scope.\n\n"
            "Cue reports a `cue.approval_requested` event per reviewer, which the "
            "server's rules turn into a notification with a one-time review link; only "
            "people decide, no tool can. Returns the request: `id`, `status` "
            "(`pending`), `title`, `body`, `reviewers`, `importance`, `metadata`, "
            "`expires_at` and `created_at`. Then poll get_approval with the `id` and act "
            "only on `approved`. Agent keys may name only their agent's configured "
            "reviewers (403 otherwise) and have a limit of open requests (429, 10 by "
            "default)."
        ),
    )
    async def request_approval(  # pyright: ignore[reportUnusedFunction]
        title: Annotated[
            str,
            Field(
                description="What needs a decision, ideally a yes/no question such as "
                "`Refund order 9?`; 1-200 characters."
            ),
        ],
        reviewers: Annotated[
            list[str],
            Field(
                description="External ids of the 1-10 people who may decide; the first "
                "decision settles the request."
            ),
        ],
        body: Annotated[
            str,
            Field(
                description="What the reviewer needs to decide (what, why, impact), up to "
                "4,000 characters. Defaults to empty."
            ),
        ] = "",
        importance: Annotated[
            ImportanceName,
            Field(
                description="How urgently reviewers are notified; defaults to `high`. "
                "An agent's ceiling lowers anything above it."
            ),
        ] = "high",
        expires_in_minutes: Annotated[
            int,
            Field(
                description="Minutes until an undecided request expires, which means no: "
                "1 to 10080 (7 days), smaller values become 1. Defaults to 60."
            ),
        ] = 60,
        metadata: Annotated[
            dict[str, Any] | None,
            Field(
                description="JSON object returned unchanged with the request and its "
                'decision, e.g. `{"order": "9"}`, to tie it back to your work; at most '
                "16 KB. Defaults to {}."
            ),
        ] = None,
    ) -> Any:
        payload = {
            "title": title,
            "body": body,
            "reviewers": reviewers,
            "importance": importance,
            "expires_in_seconds": max(60, expires_in_minutes * 60),
            "metadata": metadata or {},
        }
        return _result(await client.post("/v1/approvals", json=payload))

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Fetch an approval request by id to learn whether a person approved the "
            "action you asked about with request_approval. Call it again, with a pause, "
            "while `status` is `pending`. Needs the `messages:read` scope.\n\n"
            "Returns `id`, `status` (`pending`, `approved`, `rejected` or `expired`; a "
            "request past `expires_at` reads as `expired`), `title`, `body`, "
            "`reviewers`, `importance`, `agent`, `requested_by`, `metadata`, `comment` "
            "(the reviewer's note, if any), `decided_by`, `decided_at`, `expires_at` and "
            "`created_at`. Treat anything but `approved` as no. Fails with 404 when the "
            "id does not exist or, for an agent key, belongs to another agent."
        ),
    )
    async def get_approval(  # pyright: ignore[reportUnusedFunction]
        approval_id: Annotated[
            str,
            Field(description="Approval request UUID: the `id` returned by request_approval."),
        ],
    ) -> Any:
        return _result(await client.get(f"/v1/approvals/{_id(approval_id)}"))

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Fetch a reported event by id and, for each rule listening for its name, "
            "whether the rule fired and what came of it. Use it after send_event to learn "
            "what was sent and why or why not; follow a `message_id` with get_message for "
            "delivery, and use explain for a dry run instead. Needs the `messages:read` "
            "scope.\n\n"
            "Returns `id`, `name`, `source`, `idempotency_key`, `data`, `status` "
            "(`pending` until the worker runs the rules, then `processed`, or `failed` "
            "with the reason in `error`), `occurred_at`, `received_at` and `outcome`: "
            "null while pending, else one entry per rule with `rule`, `matched`, "
            "`reason`, `status`, `message_id` and `digest_id`. `matched: false` comes with "
            "`condition_false`, `outside_rollout` or `condition_error`. A composed message "
            "has `status` and `message_id`, and `reason` is its `status_reason` when "
            "suppressed (e.g. `agent_budget: …`, `unsubscribed`); `digested` means it "
            "joined `digest_id`; `agent_not_allowed` means the agent may not use the "
            "rule's category. An empty list means no rule listens for the name. Fails "
            "with 404 when the id does not exist or, for an agent key, was reported by "
            "another sender."
        ),
    )
    async def get_event(  # pyright: ignore[reportUnusedFunction]
        event_id: Annotated[
            str,
            Field(
                description="Event UUID: the `id` returned by send_event, or a message's "
                "`event_id` from get_message."
            ),
        ],
    ) -> Any:
        return _result(await client.get(f"/v1/events/{_id(event_id)}"))

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Dry-run an event for a person: which rules would fire, what each would do "
            "and why the others would not. Use it before send_event to check that a rule "
            "will reach the person; nothing is stored or sent. Needs an admin key, so "
            "agent keys get 403 and should read get_event for a real event instead.\n\n"
            "Returns `rules`, one entry per rule listening for the name, with `rule`, "
            "`matched`, `status` (what the message would get, e.g. `queued` or "
            "`suppressed`), `reason` and `digest_id`, read like get_event's `outcome`. "
            "Fails with 404 when `recipient` does not exist yet."
        ),
    )
    async def explain(  # pyright: ignore[reportUnusedFunction]
        event_name: Annotated[
            str,
            Field(description="Event name to test, as for send_event, e.g. `order.shipped`."),
        ],
        recipient: Annotated[
            str,
            Field(
                description="External id of an existing recipient; unlike send_event, "
                "an unknown id is not created."
            ),
        ],
        data: Annotated[
            dict[str, Any] | None,
            Field(description="Event details to test conditions with, as for send_event."),
        ] = None,
    ) -> Any:
        body = {"name": event_name, "recipient": recipient, "data": data or {}}
        return {"rules": _result(await client.post("/v1/rules/evaluate", json=body))}

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "Render a template with sample data to see the exact text a person would "
            "get, without sending anything. Use it to check which `data` a template "
            "needs before send_message; list_templates gives the keys. Needs an admin "
            "key, so agent keys get 403.\n\n"
            "Returns `locale` (the one used) and `variants`: the rendered `title`, "
            "`body`, `url` and other content under `default` and under each channel with "
            "its own override. Fails with 404 for an unknown template and 422 when it "
            "cannot be rendered."
        ),
    )
    async def preview_template(  # pyright: ignore[reportUnusedFunction]
        template: Annotated[
            str,
            Field(description="Template key, e.g. `order-shipped`, from list_templates."),
        ],
        data: Annotated[
            dict[str, Any] | None,
            Field(description="Sample template variables, read as `data`. Defaults to {}."),
        ] = None,
        locale: Annotated[
            str | None,
            Field(
                description="Locale to render, e.g. `uz`. Omitted, the server's default "
                "is tried; a locale the template lacks falls back to its default."
            ),
        ] = None,
    ) -> Any:
        body = {"data": data or {}, "locale": locale}
        return _result(
            await client.post(f"/v1/templates/{quote(template, safe='')}/preview", json=body)
        )

    @server.tool(
        annotations=READ_ONLY,
        description=(
            "List the templates send_message can use, to find a template key. Use "
            "preview_template to see one's content. Needs an admin key, so agent keys "
            "get 403.\n\n"
            "Returns `templates`, up to 200, each with `key`, `description` and "
            "`locales`."
        ),
    )
    async def list_templates() -> Any:  # pyright: ignore[reportUnusedFunction]
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
