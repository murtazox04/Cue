"""Screening agents' messages before they reach people."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from cue_notify.ai.screening import ScreeningError, ScreeningRequest, Verdict
from cue_notify.api.app import create_app
from cue_notify.core.enums import Importance
from cue_notify.runtime import Runtime
from tests.helpers import FakeChannel, create_recipient, create_template, drain


@dataclass
class FakeScreener:
    verdict: Verdict = field(default_factory=Verdict)
    error: Exception | None = None
    requests: list[ScreeningRequest] = field(default_factory=list)

    async def screen(self, request: ScreeningRequest) -> Verdict:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.verdict


@pytest.fixture
def screener(runtime: Runtime) -> FakeScreener:
    fake = FakeScreener()
    runtime.screener = fake
    return fake


@pytest.fixture
async def agent(
    client: httpx.AsyncClient, runtime: Runtime
) -> AsyncIterator[Callable[..., Awaitable[httpx.AsyncClient]]]:
    await create_template(client)
    await create_recipient(client)
    clients: list[httpx.AsyncClient] = []

    async def connect(**fields: Any) -> httpx.AsyncClient:
        body = {"key": "sales-bot", "name": "Sales", "description": "Follows up on trials"}
        assert (await client.post("/v1/agents", json={**body, **fields})).status_code == 201
        key = await client.post(
            "/v1/api-keys",
            json={"name": "sales-key", "scopes": ["messages:write"], "agent": "sales-bot"},
        )
        app = create_app(runtime=runtime)
        app.state.runtime = runtime
        connected = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://cue.test",
            headers={"Authorization": f"Bearer {key.json()['key']}"},
        )
        clients.append(connected)
        return connected

    yield connect
    for connected in clients:
        await connected.aclose()


async def _send(http: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    body = {
        "recipient": "user-1",
        "template": "order-shipped",
        "channels": ["push"],
        "data": {"order_id": "A-1"},
        **fields,
    }
    response = await http.post("/v1/messages", json=body)
    assert response.status_code == 202, response.text
    return response.json()


async def test_risky_messages_wait_for_a_person(
    client: httpx.AsyncClient,
    runtime: Runtime,
    push: FakeChannel,
    screener: FakeScreener,
    agent: Any,
) -> None:
    screener.verdict = Verdict(hold="pressure", record={"risks": {"pressure": 0.9}})
    bot = await agent(reviewers=["user-1"])
    sent = await _send(bot)
    assert sent["status"] == "queued"  # screening runs off the request path
    await drain(runtime)
    assert push.sent == []

    held = (await client.get(f"/v1/messages/{sent['id']}")).json()
    assert (held["status"], held["status_reason"]) == ("pending_approval", "screening: pressure")
    assert held["screening"]["outcome"] == "held"
    assert held["expires_at"] is not None
    assert screener.requests[0].agent_description == "Follows up on trials"
    events = await client.get("/v1/events", params={"name": "cue.approval_requested"})
    reviews = events.json()["items"]
    assert len(reviews) == 1

    approved = await client.post(f"/v1/messages/{sent['id']}/approve")
    assert approved.status_code == 200, approved.text
    await drain(runtime)
    delivered = (await client.get(f"/v1/messages/{sent['id']}")).json()
    assert delivered["status"] == "sent"
    assert len(screener.requests) == 1  # a person's approval is not second-guessed


async def test_messages_of_a_disabled_agent_are_stopped_before_screening(
    client: httpx.AsyncClient,
    runtime: Runtime,
    push: FakeChannel,
    screener: FakeScreener,
    agent: Any,
) -> None:
    bot = await agent()
    sent = await _send(bot)
    response = await client.patch("/v1/agents/sales-bot", json={"enabled": False})
    assert response.status_code == 200, response.text
    await drain(runtime)

    stopped = (await client.get(f"/v1/messages/{sent['id']}")).json()
    assert (stopped["status"], stopped["status_reason"]) == ("suppressed", "agent_not_allowed")
    assert stopped["screening"] is None
    assert screener.requests == []
    assert push.sent == []


async def test_overstated_importance_is_lowered(
    client: httpx.AsyncClient,
    runtime: Runtime,
    push: FakeChannel,
    screener: FakeScreener,
    agent: Any,
) -> None:
    screener.verdict = Verdict(importance=Importance.LOW, record={"lowered_from": "high"})
    bot = await agent()
    sent = await _send(bot, importance="high")
    await drain(runtime)
    assert [d.importance for d in push.sent] == [Importance.LOW]
    stored = (await client.get(f"/v1/messages/{sent['id']}")).json()
    assert (stored["status"], stored["importance"]) == ("sent", "low")
    assert stored["screening"]["outcome"] == "lowered"


async def test_screening_never_receives_the_preference_token(
    client: httpx.AsyncClient, runtime: Runtime, screener: FakeScreener, agent: Any
) -> None:
    await create_template(
        client, key="manage", locales={"en": {"body": "Manage: {{ links.token }}"}}
    )
    bot = await agent()
    await _send(bot, template="manage")
    await drain(runtime)
    token = (await client.get("/v1/recipients/user-1")).json()["preference_token"]
    assert token
    [request] = screener.requests
    assert token not in str(request.state())
    assert request.content["default"]["body"] == "Manage: [preference-token]"


async def test_your_own_messages_are_not_screened(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, screener: FakeScreener
) -> None:
    await create_template(client)
    await create_recipient(client)
    await _send(client)
    await drain(runtime)
    assert len(push.sent) == 1
    assert screener.requests == []


@pytest.mark.parametrize(("on_error", "status"), [("hold", "pending_approval"), ("send", "sent")])
async def test_screening_failures_follow_on_error(
    client: httpx.AsyncClient,
    runtime: Runtime,
    screener: FakeScreener,
    agent: Any,
    on_error: str,
    status: str,
) -> None:
    runtime.settings.screening.on_error = on_error  # type: ignore[assignment]
    screener.error = ScreeningError("HTTP 503")
    bot = await agent()
    sent = await _send(bot)
    await drain(runtime)
    stored = (await client.get(f"/v1/messages/{sent['id']}")).json()
    assert stored["status"] == status
    assert stored["screening"]["error"] == "HTTP 503"
