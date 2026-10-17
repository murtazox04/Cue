"""The MCP server drives the real API."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from cue_notify.mcp_server import INSTRUCTIONS, create_server
from cue_notify.runtime import Runtime
from tests.helpers import FakeChannel, create_recipient, create_rule, create_template, drain


async def _call(server: Any, tool: str, **arguments: Any) -> Any:
    result = await server.call_tool(tool, arguments)
    assert not result.is_error, result
    return json.loads(result.content[0].text)


async def test_agent_can_notify_and_inspect(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_template(client)
    await create_rule(client)
    await create_recipient(client)
    server = create_server(client)

    tools = {tool.name: tool for tool in await server.list_tools()}
    assert {"send_event", "send_message", "explain", "list_messages"} <= set(tools)
    assert tools["get_message"].annotations.read_only_hint is True
    assert "review_message" not in tools

    explanation = await _call(
        server, "explain", event_name="order.shipped", recipient="user-1", data={"order_id": "9"}
    )
    assert explanation["rules"][0]["status"] == "queued"

    event = await _call(
        server,
        "send_event",
        name="order.shipped",
        recipient="user-1",
        data={"order_id": "9"},
        idempotency_key="agent-1",
    )
    assert event["duplicate"] is False
    await drain(runtime)
    assert push.sent[0].content.body == "Order 9 is on its way."

    messages = await _call(server, "list_messages", recipient="user-1", status="sent")
    assert len(messages["items"]) == 1
    preview = await _call(
        server, "preview_template", template="order-shipped", data={"order_id": "1"}
    )
    assert preview["variants"]["sms"]["body"] == "Order 1 shipped."
    templates = await _call(server, "list_templates")
    assert templates == {
        "templates": [{"key": "order-shipped", "description": None, "locales": ["en", "uz"]}]
    }


async def test_initialize_returns_instructions_and_tools_describe_every_parameter(
    client: httpx.AsyncClient,
) -> None:
    async with Client(create_server(client)) as session:
        assert session.instructions == INSTRUCTIONS
        tools = (await session.list_tools()).tools

    assert len(tools) == 10
    for tool in tools:
        assert tool.description, tool.name
        assert tool.description[0].isupper(), tool.name
        for name, schema in tool.input_schema.get("properties", {}).items():
            assert schema.get("description"), f"{tool.name}.{name} has no description"


async def test_api_errors_become_tool_errors(client: httpx.AsyncClient) -> None:
    server = create_server(client)
    with pytest.raises(ToolError, match="404"):
        await server.call_tool(
            "send_message", {"recipient": "u", "template": "missing", "channels": ["push"]}
        )


async def test_ids_cannot_reach_other_endpoints(client: httpx.AsyncClient) -> None:
    server = create_server(client)
    with pytest.raises(ToolError, match="not a valid id"):
        await server.call_tool("get_message", {"message_id": "../recipients/user-1"})
    with pytest.raises(ToolError, match="not a valid id"):
        await server.call_tool("get_approval", {"approval_id": "../recipients/user-1"})


async def test_agent_asks_a_person_before_acting(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await create_recipient(client, external_id="lead")
    server = create_server(client)
    asked = await _call(
        server,
        "request_approval",
        title="Refund order 9?",
        reviewers=["lead"],
        body="Customer says it never arrived.",
        expires_in_minutes=30,
        metadata={"order": "9"},
    )
    assert asked["status"] == "pending"
    assert asked["metadata"] == {"order": "9"}
    approved = await client.post(f"/v1/approvals/{asked['id']}/approve")
    assert approved.status_code == 200
    decided = await _call(server, "get_approval", approval_id=asked["id"])
    assert decided["status"] == "approved"
