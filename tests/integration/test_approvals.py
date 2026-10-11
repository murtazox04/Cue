"""Approval requests: an agent asks a person to decide; Cue reports the decision back."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from cue_notify.api.app import create_app
from cue_notify.channels.webhook import sign
from cue_notify.db.models import Approval, Job
from cue_notify.runtime import Runtime
from cue_notify.services import approvals
from cue_notify.settings import (
    ApiSettings,
    ApprovalSettings,
    DatabaseSettings,
    PolicySettings,
    Settings,
    WorkerSettings,
)
from tests.helpers import FakeChannel, create_recipient, drain

BASE = "https://notify.test"
CALLBACK = "https://app.test/cue/decisions"
SECRET = "callback-secret"


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        environment="test",
        database=DatabaseSettings(url=database_url, pool_size=5),
        worker=WorkerSettings(concurrency=4, retry_base_seconds=1, retry_max_seconds=2),
        policy=PolicySettings(default_locale="en", default_timezone="UTC", quiet_hours=None),
        api=ApiSettings(public_url=f"{BASE}/"),
        approvals=ApprovalSettings(callback_url=CALLBACK, callback_secret=SECRET),
    )


@pytest.fixture
def callbacks() -> list[httpx.Request]:
    return []


@pytest.fixture
async def runtime(
    settings: Settings, push: FakeChannel, sms: FakeChannel, callbacks: list[httpx.Request]
) -> AsyncIterator[Runtime]:
    def handler(request: httpx.Request) -> httpx.Response:
        callbacks.append(request)
        return httpx.Response(204)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rt = Runtime.create(settings, channels={"push": push, "sms": sms}, http=http)
    try:
        yield rt
    finally:
        await rt.aclose()


def _client(runtime: Runtime, key: str | None = None) -> httpx.AsyncClient:
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cue.test", headers=headers
    )


@pytest.fixture
async def agent(client: httpx.AsyncClient, runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    """The ops agent's client, after registering the agent and its key."""
    await create_recipient(
        client, external_id="lead", addresses=[{"channel": "push", "value": "p"}]
    )
    body = {
        "key": "ops-bot",
        "name": "Ops assistant",
        "max_importance": "high",
        "reviewers": ["lead"],
    }
    assert (await client.post("/v1/agents", json=body)).status_code == 201
    key = await client.post(
        "/v1/api-keys",
        json={"name": "ops-key", "scopes": ["messages:write", "messages:read"], "agent": "ops-bot"},
    )
    async with _client(runtime, key.json()["key"]) as http:
        yield http


async def _ask(agent: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    body = {
        "title": "Delete 1,204 inactive accounts?",
        "body": "Accounts with no login since 2024. This cannot be undone.",
        "reviewers": ["lead"],
        "metadata": {"flow_id": "flow-7"},
        **fields,
    }
    response = await agent.post("/v1/approvals", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _review_path(client: httpx.AsyncClient) -> str:
    events = (await client.get("/v1/events", params={"name": "cue.approval_requested"})).json()
    [event] = events["items"]
    assert event["data"]["kind"] == "action"
    return event["data"]["review_url"].removeprefix(BASE)


async def test_a_person_approves_and_the_requester_hears_back(
    client: httpx.AsyncClient,
    agent: httpx.AsyncClient,
    runtime: Runtime,
    callbacks: list[httpx.Request],
) -> None:
    asked = await _ask(agent, importance="critical")
    assert (asked["status"], asked["agent"], asked["importance"]) == ("pending", "ops-bot", "high")
    await drain(runtime)

    path = await _review_path(client)
    async with _client(runtime) as browser:
        page = await browser.get(path)
        assert page.status_code == 200
        assert "Delete 1,204 inactive accounts?" in page.text
        assert "Ops assistant" in page.text
        done = await browser.post(path, data={"action": "approve", "reason": "go ahead"})
        assert done.status_code == 200
    await drain(runtime)

    decided = (await agent.get(f"/v1/approvals/{asked['id']}")).json()
    assert (decided["status"], decided["decided_by"], decided["comment"]) == (
        "approved",
        "link:lead",
        "go ahead",
    )
    [request] = callbacks
    assert str(request.url) == CALLBACK
    payload = json.loads(request.content)
    assert payload == {
        "type": "approval.decided",
        "id": asked["id"],
        "status": "approved",
        "title": "Delete 1,204 inactive accounts?",
        "agent": "ops-bot",
        "decided_by": "link:lead",
        "comment": "go ahead",
        "decided_at": payload["decided_at"],
        "metadata": {"flow_id": "flow-7"},
    }
    timestamp = int(request.headers["Cue-Signature"].split(",")[0].removeprefix("t="))
    assert request.headers["Cue-Signature"] == sign(request.content, SECRET, timestamp)


async def test_people_decide_through_the_api_but_agents_cannot(
    client: httpx.AsyncClient,
    agent: httpx.AsyncClient,
    runtime: Runtime,
    callbacks: list[httpx.Request],
) -> None:
    asked = await _ask(agent)
    own = await agent.post(f"/v1/approvals/{asked['id']}/approve")
    assert own.status_code == 403

    rejected = await client.post(
        f"/v1/approvals/{asked['id']}/reject", json={"reason": "not this week"}
    )
    assert rejected.status_code == 200, rejected.text
    assert (rejected.json()["status"], rejected.json()["comment"]) == ("rejected", "not this week")
    again = await client.post(f"/v1/approvals/{asked['id']}/approve")
    assert again.status_code == 409
    await drain(runtime)
    assert [json.loads(r.content)["status"] for r in callbacks] == ["rejected"]


async def test_undecided_requests_expire_as_a_no(
    client: httpx.AsyncClient,
    agent: httpx.AsyncClient,
    runtime: Runtime,
    callbacks: list[httpx.Request],
) -> None:
    asked = await _ask(agent, expires_in_seconds=60)
    await drain(runtime)
    path = await _review_path(client)
    past = datetime.now(UTC) - timedelta(seconds=1)
    async with runtime.transaction() as session:
        await session.execute(update(Approval).values(expires_at=past))
        await session.execute(
            update(Job).where(Job.kind == approvals.EXPIRE_JOB).values(run_at=past)
        )
    await drain(runtime)

    expired = (await agent.get(f"/v1/approvals/{asked['id']}")).json()
    assert (expired["status"], expired["decided_by"]) == ("expired", None)
    assert [json.loads(r.content)["status"] for r in callbacks] == ["expired"]
    async with _client(runtime) as browser:
        late = await browser.post(path, data={"action": "approve"})
    assert late.status_code == 410


async def test_agents_only_see_their_own_requests(
    client: httpx.AsyncClient, agent: httpx.AsyncClient, runtime: Runtime
) -> None:
    asked = await _ask(agent)
    other_agent = {"key": "sales-bot", "name": "Sales"}
    assert (await client.post("/v1/agents", json=other_agent)).status_code == 201
    key = await client.post(
        "/v1/api-keys",
        json={"name": "sales-key", "scopes": ["messages:read"], "agent": "sales-bot"},
    )
    async with _client(runtime, key.json()["key"]) as sales:
        assert (await sales.get(f"/v1/approvals/{asked['id']}")).status_code == 404
        assert (await sales.get("/v1/approvals")).json()["items"] == []
    mine = (await agent.get("/v1/approvals", params={"status": "pending"})).json()["items"]
    assert [a["id"] for a in mine] == [asked["id"]]


async def test_agents_may_only_ask_their_own_reviewers(
    client: httpx.AsyncClient, agent: httpx.AsyncClient
) -> None:
    await create_recipient(client, external_id="customer-9")
    response = await agent.post(
        "/v1/approvals", json={"title": "Hi!", "reviewers": ["lead", "customer-9"]}
    )
    assert response.status_code == 403
    assert "customer-9" in response.text
    # Your own backend may ask anyone.
    ok = await client.post("/v1/approvals", json={"title": "Hi?", "reviewers": ["customer-9"]})
    assert ok.status_code == 201


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"reviewers": []}, "reviewers"),
        ({"expires_in_seconds": 10}, "expires_in_seconds"),
        ({"title": ""}, "title"),
        ({"metadata": {"blob": "x" * 20_000}}, "16"),
    ],
)
async def test_invalid_requests_are_rejected(
    agent: httpx.AsyncClient, fields: dict[str, Any], fragment: str
) -> None:
    body = {"title": "Refund?", "reviewers": ["lead"], **fields}
    response = await agent.post("/v1/approvals", json=body)
    assert response.status_code == 422
    assert fragment in response.text
