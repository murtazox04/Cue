"""One-time review links: approve or reject a held agent message from the notification."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update

from cue_notify.api.app import create_app
from cue_notify.db.models import ReviewLink
from cue_notify.runtime import Runtime
from cue_notify.settings import (
    ApiSettings,
    DatabaseSettings,
    PolicySettings,
    Settings,
    WorkerSettings,
)
from tests.helpers import TEST_SECRET_KEY, FakeChannel, create_recipient, create_template, drain

BASE = "https://notify.test"


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        environment="test",
        database=DatabaseSettings(url=database_url, pool_size=5),
        worker=WorkerSettings(concurrency=4, retry_base_seconds=1, retry_max_seconds=2),
        policy=PolicySettings(default_locale="en", default_timezone="UTC", quiet_hours=None),
        api=ApiSettings(public_url=f"{BASE}/", secret_key=TEST_SECRET_KEY),
    )


def _client(runtime: Runtime, key: str | None = None) -> httpx.AsyncClient:
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cue.test", headers=headers
    )


@pytest.fixture
async def anon(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    """A reviewer's browser: no API key, only the link."""
    async with _client(runtime) as http:
        yield http


async def _held(client: httpx.AsyncClient, runtime: Runtime, reviewers: list[str]) -> str:
    """Hold one agent message for review and return its id."""
    await create_template(client)
    await create_recipient(client)
    body = {"key": "sales-bot", "name": "Sales assistant", "approval": True, "reviewers": reviewers}
    assert (await client.post("/v1/agents", json=body)).status_code == 201
    key = await client.post(
        "/v1/api-keys",
        json={"name": "sales-key", "scopes": ["messages:write"], "agent": "sales-bot"},
    )
    async with _client(runtime, key.json()["key"]) as agent:
        sent = await agent.post(
            "/v1/messages",
            json={
                "recipient": "user-1",
                "template": "order-shipped",
                "channels": ["push"],
                "data": {"order_id": "A-1"},
            },
        )
    assert sent.json()["status"] == "pending_approval"
    await drain(runtime)  # the review job reports one event per reviewer
    return sent.json()["id"]


async def _links(client: httpx.AsyncClient) -> dict[str, str]:
    events = await client.get("/v1/events", params={"name": "cue.approval_requested"})
    return {
        event["idempotency_key"].split(":", 1)[1]: event["data"]["review_url"].removeprefix(BASE)
        for event in events.json()["items"]
    }


async def test_a_reviewer_approves_from_the_link(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    message_id = await _held(client, runtime, ["lead"])
    [(reviewer, path)] = (await _links(client)).items()
    assert reviewer == "lead"
    assert path.startswith("/review/")

    page = await anon.get(path)
    assert page.status_code == 200
    assert "Sales assistant" in page.text and "Order A-1 is on its way." in page.text
    assert page.headers["Cache-Control"] == "no-store"
    # Looking (or a mail scanner prefetching) changes nothing.
    held = (await client.get(f"/v1/messages/{message_id}")).json()
    assert held["status"] == "pending_approval"

    done = await anon.post(path, data={"action": "approve"})
    assert done.status_code == 200
    assert "on its way" in done.text
    await drain(runtime)
    message = (await client.get(f"/v1/messages/{message_id}")).json()
    assert (message["status"], message["reviewed_by"]) == ("sent", "link:lead")
    assert [d.address for d in push.sent] == ["token-1"]

    again = await anon.post(path, data={"action": "reject"})
    assert again.status_code == 410
    assert "already reviewed by link:lead" in again.text


async def test_rejecting_records_the_reason(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    message_id = await _held(client, runtime, ["lead"])
    [path] = (await _links(client)).values()
    done = await anon.post(path, data={"action": "reject", "reason": "wrong customer"})
    assert done.status_code == 200
    message = (await client.get(f"/v1/messages/{message_id}")).json()
    assert (message["status"], message["status_reason"]) == (
        "cancelled",
        "rejected: wrong customer",
    )
    await drain(runtime)
    assert push.sent == []


async def test_one_decision_closes_every_reviewers_link(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, runtime: Runtime
) -> None:
    await create_recipient(client, external_id="lead")
    await create_recipient(client, external_id="deputy")
    message_id = await _held(client, runtime, ["lead", "deputy"])
    links = await _links(client)
    assert set(links) == {"lead", "deputy"}
    assert links["lead"] != links["deputy"]

    approved = await client.post(f"/v1/messages/{message_id}/approve")
    assert approved.status_code == 200
    for path in links.values():
        page = await anon.get(path)
        assert page.status_code == 410
        assert "already reviewed by tests" in page.text


async def test_expired_and_unknown_links_do_nothing(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, runtime: Runtime
) -> None:
    message_id = await _held(client, runtime, ["lead"])
    [path] = (await _links(client)).values()
    async with runtime.transaction() as session:
        await session.execute(
            update(ReviewLink).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    expired = await anon.post(path, data={"action": "approve"})
    assert expired.status_code == 410
    assert "expired" in expired.text
    message = (await client.get(f"/v1/messages/{message_id}")).json()
    assert message["status"] == "pending_approval"

    unknown = await anon.get("/review/not-a-real-token")
    assert unknown.status_code == 404


async def test_only_a_hash_of_the_token_is_stored(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await _held(client, runtime, ["lead"])
    [path] = (await _links(client)).values()
    token = path.removeprefix("/review/")
    async with runtime.session() as session:
        [link] = (await session.scalars(select(ReviewLink))).all()
    assert len(token) >= 40
    assert token not in (link.token_hash, link.reviewer)
    assert link.reviewer == "lead"


async def test_agents_cannot_read_their_own_review_links(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    """An agent that could read the review event or the reviewer's notification could
    approve its own message, so agent keys only ever see what they sent themselves."""
    message_id = await _held(client, runtime, ["lead"])
    key = await client.post(
        "/v1/api-keys",
        json={"name": "reader", "scopes": ["messages:read"], "agent": "sales-bot"},
    )
    [review_event] = (
        await client.get("/v1/events", params={"name": "cue.approval_requested"})
    ).json()["items"]
    async with _client(runtime, key.json()["key"]) as agent:
        events = (await agent.get("/v1/events")).json()["items"]
        assert events == []
        assert (await agent.get(f"/v1/events/{review_event['id']}")).status_code == 404
        messages = (await agent.get("/v1/messages")).json()["items"]
        assert [m["id"] for m in messages] == [message_id]
        assert (await agent.get("/v1/digests")).status_code == 403


@pytest.mark.parametrize("token", ["\\\\evil.example", "%5C%5Cevil.example", "evil.example"])
async def test_an_unknown_action_redirects_back_to_the_same_page(
    anon: httpx.AsyncClient, token: str
) -> None:
    response = await anon.post(f"/review/{token}", data={"action": "steal"})
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/review/")
    assert not location.startswith("//")
