"""Agents: attention budgets, importance limits and human approval."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from cue_notify.api.app import create_app
from cue_notify.db.models import Message
from cue_notify.runtime import Runtime
from tests.helpers import FakeChannel, create_recipient, create_rule, create_template, drain


async def _agent(client: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    body = {"key": "support-bot", "name": "Support assistant", **fields}
    response = await client.post("/v1/agents", json=body)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def bot(
    client: httpx.AsyncClient, runtime: Runtime
) -> AsyncIterator[Callable[[], Awaitable[httpx.AsyncClient]]]:
    """Connects a client authenticated as the support agent (each test creates it first)."""
    await create_template(client)
    await create_recipient(client)

    async def connect(agent: str = "support-bot") -> httpx.AsyncClient:
        response = await client.post(
            "/v1/api-keys",
            json={
                "name": f"{agent}-key",
                "scopes": ["messages:write", "messages:read"],
                "agent": agent,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["agent"] == agent
        app = create_app(runtime=runtime)
        app.state.runtime = runtime
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://cue.test",
            headers={"Authorization": f"Bearer {response.json()['key']}"},
        )

    clients: list[httpx.AsyncClient] = []

    async def factory() -> httpx.AsyncClient:
        connected = await connect()
        clients.append(connected)
        return connected

    yield factory
    for connected in clients:
        await connected.aclose()


async def _send(http: httpx.AsyncClient, **fields: Any) -> httpx.Response:
    body = {
        "recipient": "user-1",
        "template": "order-shipped",
        "channels": ["push"],
        "data": {"order_id": "A-1"},
        **fields,
    }
    return await http.post("/v1/messages", json=body)


async def test_agents_have_a_per_person_budget(client: httpx.AsyncClient, bot: Any) -> None:
    await _agent(client, frequency_caps=[{"period": "P1D", "limit": 2}])
    agent = await bot()
    statuses = [(await _send(agent)).json() for _ in range(3)]
    assert [m["status"] for m in statuses] == ["queued", "queued", "suppressed"]
    assert statuses[2]["status_reason"] == "agent_budget: 2 per 1 day, 0:00:00"
    assert statuses[0]["agent"] == "support-bot"
    # Your own backend is not bound by the agent's budget.
    assert (await _send(client)).json()["status"] == "queued"
    mine = (await client.get("/v1/messages", params={"agent": "support-bot"})).json()["items"]
    assert len(mine) == 3


async def test_agents_default_to_five_messages_a_day(client: httpx.AsyncClient) -> None:
    agent = await _agent(client)
    assert agent["frequency_caps"] == [{"period": "P1D", "limit": 5}]
    assert agent["max_importance"] == "high"


async def test_categories_and_importance_are_limited(client: httpx.AsyncClient, bot: Any) -> None:
    await _agent(client, categories=["transactional"], max_importance="normal")
    agent = await bot()
    refused = await _send(agent, category="marketing")
    assert refused.status_code == 403
    assert "may not send marketing" in refused.json()["detail"]
    message = (await _send(agent, importance="critical")).json()
    assert message["importance"] == "normal"


async def test_high_importance_waits_for_a_human(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, bot: Any
) -> None:
    await _agent(client, approval={"in": [{"var": "message.importance"}, ["high", "critical"]]})
    agent = await bot()
    routine = (await _send(agent)).json()
    held = (await _send(agent, importance="high")).json()
    assert (routine["status"], held["status"]) == ("queued", "pending_approval")
    assert held["expires_at"] is not None  # unreviewed messages do not wait forever
    await drain(runtime)
    assert len(push.sent) == 1

    own = await agent.post(f"/v1/messages/{held['id']}/approve")
    assert own.status_code == 403

    approved = await client.post(f"/v1/messages/{held['id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "queued"
    assert approved.json()["reviewed_by"] == "tests"
    await drain(runtime)
    assert len(push.sent) == 2

    again = await client.post(f"/v1/messages/{held['id']}/approve")
    assert again.status_code == 409


async def test_rejected_messages_are_cancelled_with_the_reason(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, bot: Any
) -> None:
    await _agent(client, approval=True)
    agent = await bot()
    held = (await _send(agent)).json()
    response = await client.post(
        f"/v1/messages/{held['id']}/reject", json={"reason": "wrong customer"}
    )
    assert response.status_code == 200
    rejected = response.json()
    assert (rejected["status"], rejected["status_reason"]) == (
        "cancelled",
        "rejected: wrong customer",
    )
    assert rejected["reviewed_at"] is not None
    await drain(runtime)
    assert push.sent == []


async def test_approval_that_comes_too_late_drops_the_message(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, bot: Any
) -> None:
    await _agent(client, approval=True, approval_ttl_seconds=60)
    agent = await bot()
    held = (await _send(agent)).json()
    async with runtime.transaction() as session:
        await session.execute(
            update(Message).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    late = (await client.post(f"/v1/messages/{held['id']}/approve")).json()
    assert (late["status"], late["status_reason"]) == ("suppressed", "expired")
    await drain(runtime)
    assert push.sent == []


async def test_held_messages_count_against_the_budget(client: httpx.AsyncClient, bot: Any) -> None:
    await _agent(client, approval=True, frequency_caps=[{"period": "P1D", "limit": 1}])
    agent = await bot()
    first = (await _send(agent)).json()
    second = (await _send(agent)).json()
    assert first["status"] == "pending_approval"
    assert second["status_reason"].startswith("agent_budget")


async def test_disabling_an_agent_stops_it_at_once(client: httpx.AsyncClient, bot: Any) -> None:
    await _agent(client)
    agent = await bot()
    await client.patch("/v1/agents/support-bot", json={"enabled": False})
    response = await _send(agent)
    assert response.status_code == 403
    assert "disabled" in response.json()["detail"]


async def test_agents_with_keys_cannot_be_deleted(client: httpx.AsyncClient, bot: Any) -> None:
    await _agent(client)
    await bot()
    assert (await client.delete("/v1/agents/support-bot")).status_code == 409
    unknown = await client.post(
        "/v1/api-keys", json={"name": "x", "scopes": ["messages:write"], "agent": "ghost"}
    )
    assert unknown.status_code == 404


async def test_approval_conditions_are_validated(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/agents", json={"key": "bad", "name": "Bad", "approval": {"nope": [1]}}
    )
    assert response.status_code == 422


async def _key(client: httpx.AsyncClient, agent: str, scopes: list[str]) -> httpx.Response:
    return await client.post(
        "/v1/api-keys", json={"name": f"{agent}-{len(scopes)}", "scopes": scopes, "agent": agent}
    )


async def test_agent_keys_cannot_be_admin(client: httpx.AsyncClient) -> None:
    await _agent(client)
    response = await _key(client, "support-bot", ["admin"])
    assert response.status_code == 422
    assert "admin" in response.json()["detail"]


async def test_agents_cannot_review_or_touch_preferences(
    client: httpx.AsyncClient, runtime: Runtime, bot: Any
) -> None:
    await _agent(client, approval=True)
    agent = await bot()
    held = (await _send(agent)).json()

    await _agent(client, key="other-bot")
    other = await _key(client, "other-bot", ["messages:write"])
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://cue.test",
        headers={"Authorization": f"Bearer {other.json()['key']}"},
    ) as peer:
        # Another agent cannot approve: two agents could otherwise approve each other.
        assert (await peer.post(f"/v1/messages/{held['id']}/approve")).status_code == 403
        resubscribe = await peer.put(
            "/v1/recipients/user-1/preferences", json={"unsubscribed_categories": []}
        )
        assert resubscribe.status_code == 403
        rotate = await peer.post("/v1/recipients/user-1/preference-token")
        assert rotate.status_code == 403


# --------------------------------------------------------------------------- audit findings


def _as(runtime: Runtime, raw: str) -> httpx.AsyncClient:
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://cue.test",
        headers={"Authorization": f"Bearer {raw}"},
    )


async def _agent_client(
    client: httpx.AsyncClient, runtime: Runtime, scopes: list[str], **fields: Any
) -> httpx.AsyncClient:
    await create_template(client)
    await create_recipient(client)
    await _agent(client, **fields)
    response = await _key(client, "support-bot", scopes)
    assert response.status_code == 201, response.text
    return _as(runtime, response.json()["key"])


async def test_backdated_sends_still_count_against_the_budget(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    async with await _agent_client(
        client, runtime, ["messages:write"], frequency_caps=[{"period": "P1D", "limit": 2}]
    ) as agent:
        sent = []
        for days in (100, 99, 98):
            at = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            sent.append((await _send(agent, send_at=at)).json())
    assert [m["status"] for m in sent] == ["queued", "queued", "suppressed"]


async def test_agents_never_see_preference_tokens(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await create_template(
        client,
        key="promo",
        locales={"en": {"body": "Manage: {{ links.token }}"}},
    )
    async with await _agent_client(client, runtime, ["messages:write", "messages:read"]) as agent:
        assert (await agent.get("/v1/recipients/user-1")).status_code == 403
        token = (await client.get("/v1/recipients/user-1")).json()["preference_token"]
        assert token

        sent = (await _send(agent, template="promo", category="transactional")).json()
        assert sent["content"]["default"]["body"] == "Manage: [redacted]"
        fetched = (await agent.get(f"/v1/messages/{sent['id']}")).json()
        assert token not in str(fetched)
        # Your backend still sees the real content.
        mine = (await client.get(f"/v1/messages/{sent['id']}")).json()
        assert mine["content"]["default"]["body"] == f"Manage: {token}"

        assert (await agent.delete("/v1/recipients/user-1")).status_code == 403


async def test_events_from_agents_follow_agent_policy(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    async with await _agent_client(
        client,
        runtime,
        ["events:write"],
        categories=["transactional"],
        frequency_caps=[{"period": "P1D", "limit": 1}],
    ) as agent:
        await create_rule(client, key="allowed", channels=["push"], stop_processing=False)
        await create_rule(client, key="promo", category="marketing", channels=["push"])
        ids = []
        for n in range(2):
            response = await agent.post(
                "/v1/events",
                json={
                    "name": "order.shipped",
                    "recipient": "user-1",
                    "data": {"order_id": f"A-{n}"},
                },
            )
            assert response.status_code == 202, response.text
            ids.append(response.json()["id"])
        inline = await agent.post(
            "/v1/events",
            json={
                "name": "order.shipped",
                "recipient": {"external_id": "user-1", "attributes": {"tier": "vip"}},
            },
        )
        assert inline.status_code == 403
    await drain(runtime)
    first = (await client.get(f"/v1/events/{ids[0]}")).json()["outcome"]
    second = (await client.get(f"/v1/events/{ids[1]}")).json()["outcome"]
    assert {o["rule"]: o.get("reason") for o in first} == {
        "allowed": None,
        "promo": "agent_not_allowed",
    }
    assert second[0]["reason"].startswith("agent_budget")


async def test_agents_cannot_cancel_or_mark_other_messages(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    async with await _agent_client(client, runtime, ["messages:write", "events:write"]) as agent:
        mine = (await _send(client)).json()
        own = (await _send(agent)).json()
        assert (await agent.post(f"/v1/messages/{mine['id']}/cancel")).status_code == 403
        assert (await agent.post(f"/v1/messages/{own['id']}/cancel")).status_code == 200
        engagement = await agent.post(
            f"/v1/messages/{mine['id']}/engagement", json={"type": "opened"}
        )
        assert engagement.status_code == 403
        inline = await _send(agent, recipient={"external_id": "user-1", "timezone": "Asia/Tokyo"})
        assert inline.status_code == 403


async def test_reviewers_are_told_through_your_own_rules(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, bot: Any
) -> None:
    await _agent(client, approval=True, reviewers=["reviewer-1"])
    await create_recipient(
        client, external_id="reviewer-1", addresses=[{"channel": "push", "value": "lead-phone"}]
    )
    await create_template(
        client,
        key="review",
        locales={
            "en": {
                "body": "{{ data.agent_name }} wants to tell {{ data.recipient }}: {{ data.body }}"
            }
        },
    )
    await create_rule(
        client,
        key="review",
        event="cue.approval_requested",
        template="review",
        channels=["push"],
    )
    agent = await bot()
    held = (await _send(agent)).json()
    assert held["status"] == "pending_approval"
    await drain(runtime)

    [notice] = push.sent
    assert notice.address == "lead-phone"
    assert notice.content.body == (
        "Support assistant wants to tell user-1: Order A-1 is on its way."
    )
    events = (await client.get("/v1/events", params={"name": "cue.approval_requested"})).json()
    assert events["items"][0]["data"]["message_id"] == held["id"]
    assert events["items"][0]["source"] == "cue"


def quiet_around_now() -> dict[str, str]:
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=2)).time().replace(second=0, microsecond=0)
    return {"start": start.isoformat(), "end": end.isoformat()}


async def test_approved_messages_wait_for_quiet_hours_to_end(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, bot: Any
) -> None:
    await _agent(client, approval=True)
    agent = await bot()
    held = (await _send(agent, category="marketing")).json()
    assert held["status"] == "pending_approval"
    await client.put("/v1/recipients/user-1/preferences", json={"quiet_hours": quiet_around_now()})

    approved = (await client.post(f"/v1/messages/{held['id']}/approve")).json()
    assert approved["status"] == "queued"
    assert datetime.fromisoformat(approved["send_at"]) > datetime.now(UTC) + timedelta(minutes=90)
    await drain(runtime)
    assert push.sent == []


async def test_approval_that_quiet_hours_would_push_past_expiry_drops_the_message(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, bot: Any
) -> None:
    await _agent(client, approval=True, approval_ttl_seconds=600)
    agent = await bot()
    held = (await _send(agent, category="marketing")).json()
    await client.put("/v1/recipients/user-1/preferences", json={"quiet_hours": quiet_around_now()})
    late = (await client.post(f"/v1/messages/{held['id']}/approve")).json()
    assert (late["status"], late["status_reason"]) == ("suppressed", "expired")
    await drain(runtime)
    assert push.sent == []
