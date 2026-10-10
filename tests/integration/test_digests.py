"""Digests: bursts of events grouped into one message per recipient and key."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from cue_notify.api.app import create_app
from cue_notify.core.enums import Scope
from cue_notify.runtime import Runtime
from cue_notify.services import api_keys
from cue_notify.settings import AISettings, Settings
from tests.helpers import FakeChannel, FakeGenerator, create_recipient, drain

COMMENTS_TEMPLATE: dict[str, Any] = {
    "key": "comments",
    "locales": {
        "en": {
            "title": "{{ data.count }} new comments",
            "body": (
                "{% for c in data.events %}{{ c.author }}{% if not loop.last %}, {% endif %}"
                "{% endfor %} commented on {{ data.last.post }}"
            ),
            "thread": "post-{{ data.last.post_id }}",
        }
    },
}


async def _setup(client: httpx.AsyncClient, category: str = "transactional", **digest: Any) -> None:
    response = await client.post("/v1/templates", json=COMMENTS_TEMPLATE)
    assert response.status_code == 201, response.text
    response = await client.post(
        "/v1/rules",
        json={
            "key": "comment-digest",
            "name": "Group comments per post",
            "event": "comment.created",
            "template": "comments",
            "category": category,
            "channels": ["push"],
            "digest": {"window": "PT10M", "key": "{{ event.data.post_id }}", **digest},
        },
    )
    assert response.status_code == 201, response.text
    await create_recipient(client, addresses=[{"channel": "push", "value": "tok"}])


async def _comment(
    client: httpx.AsyncClient, author: str, post_id: int = 1, **fields: Any
) -> dict[str, Any]:
    response = await client.post(
        "/v1/events",
        json={
            "name": "comment.created",
            "recipient": "user-1",
            "data": {"author": author, "post_id": post_id, "post": f"Post {post_id}"},
            **fields,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


async def _digests(client: httpx.AsyncClient, **params: str) -> list[dict[str, Any]]:
    response = await client.get("/v1/digests", params=params)
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def test_burst_becomes_one_message(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await _setup(client)
    events = [await _comment(client, author) for author in ("Ann", "Bo", "Cy")]
    await drain(runtime)
    assert push.sent == []  # the window is still open

    [digest] = await _digests(client)
    assert digest["status"] == "open"
    assert digest["count"] == 3
    assert digest["key"] == "1"
    assert [item["data"]["author"] for item in digest["items"]] == ["Ann", "Bo", "Cy"]

    outcome = (await client.get(f"/v1/events/{events[0]['id']}")).json()["outcome"][0]
    assert outcome == {
        "rule": "comment-digest",
        "matched": True,
        "reason": "digested",
        "digest_id": digest["id"],
    }

    response = await client.post(f"/v1/digests/{digest['id']}/send")
    assert response.status_code == 200, response.text
    await drain(runtime)

    [delivered] = push.sent
    assert delivered.content.title == "3 new comments"
    assert delivered.content.body == "Ann, Bo, Cy commented on Post 1"
    assert delivered.content.thread == "post-1"

    sent = (await client.get(f"/v1/digests/{digest['id']}")).json()
    assert sent["status"] == "sent"
    assert sent["sent_at"] is not None
    message = (await client.get(f"/v1/messages/{sent['message_id']}")).json()
    assert message["status"] == "sent"

    # The next event opens a fresh digest rather than joining the sent one.
    await _comment(client, "Di")
    await drain(runtime)
    assert [d["status"] for d in await _digests(client, status="open")] == ["open"]


async def test_grouping_key_separates_digests(client: httpx.AsyncClient, runtime: Runtime) -> None:
    await _setup(client)
    await _comment(client, "Ann", post_id=1)
    await _comment(client, "Bo", post_id=2)
    await _comment(client, "Cy", post_id=1)
    await drain(runtime)
    counts = {d["key"]: d["count"] for d in await _digests(client, recipient="user-1")}
    assert counts == {"1": 2, "2": 1}


async def test_send_after_flushes_before_the_window_ends(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await _setup(client, send_after=2)
    await _comment(client, "Ann")
    await drain(runtime)
    assert push.sent == []
    await _comment(client, "Bo")
    await drain(runtime)
    assert [d.content.title for d in push.sent] == ["2 new comments"]


async def test_max_items_keeps_the_latest_and_counts_the_rest(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await _setup(client, max_items=2, send_after=3)
    for author in ("Ann", "Bo", "Cy"):
        await _comment(client, author)
        await drain(runtime)
    [delivered] = push.sent
    assert delivered.content.title == "3 new comments"
    assert delivered.content.body == "Bo, Cy commented on Post 1"


async def test_disabled_rule_cancels_the_digest(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await _setup(client)
    await _comment(client, "Ann")
    await drain(runtime)
    [digest] = await _digests(client)
    response = await client.patch("/v1/rules/comment-digest", json={"enabled": False})
    assert response.status_code == 200, response.text
    await client.post(f"/v1/digests/{digest['id']}/send")
    await drain(runtime)
    assert push.sent == []
    assert (await client.get(f"/v1/digests/{digest['id']}")).json()["status"] == "cancelled"


async def test_dry_run_reports_without_grouping(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await _setup(client)
    response = await client.post(
        "/v1/rules/evaluate",
        json={
            "name": "comment.created",
            "recipient": "user-1",
            "data": {"author": "Ann", "post_id": 1, "post": "Post 1"},
        },
    )
    assert response.status_code == 200, response.text
    [outcome] = response.json()
    assert outcome["reason"] == "digested"
    assert outcome["digest_id"] is None
    await drain(runtime)
    assert await _digests(client) == []


async def test_policy_still_applies_to_the_digest_message(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await _setup(client, category="marketing")
    await _comment(client, "Ann")
    await drain(runtime)
    await client.put(
        "/v1/recipients/user-1/preferences", json={"unsubscribed_categories": ["marketing"]}
    )
    [digest] = await _digests(client)
    await client.post(f"/v1/digests/{digest['id']}/send")
    await drain(runtime)
    assert push.sent == []
    sent = (await client.get(f"/v1/digests/{digest['id']}")).json()
    message = (await client.get(f"/v1/messages/{sent['message_id']}")).json()
    assert message["status"] == "suppressed"


@pytest.mark.parametrize(
    ("digest", "fragment"),
    [
        ({"window": "PT10M", "summarize": True}, "summary_fields"),
        ({"window": "PT10M", "key": "{{ event.data.post_id "}, "key"),
        ({"window": "P8D"}, "window"),
        ({"window": "PT10M", "send_after": 1}, "send_after"),
    ],
)
async def test_invalid_digest_policies_are_rejected(
    client: httpx.AsyncClient, digest: dict[str, Any], fragment: str
) -> None:
    response = await client.post("/v1/templates", json=COMMENTS_TEMPLATE)
    assert response.status_code == 201
    response = await client.post(
        "/v1/rules",
        json={
            "key": "bad",
            "name": "Bad",
            "event": "comment.created",
            "template": "comments",
            "category": "transactional",
            "channels": ["push"],
            "digest": digest,
        },
    )
    assert response.status_code == 422
    assert fragment in response.text


async def test_unknown_digest_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/digests/0190a0c4-0000-7000-8000-000000000000")
    assert response.status_code == 404


# --------------------------------------------------------------------------- smart mode


@pytest.fixture
async def smart(
    settings: Settings, push: FakeChannel
) -> AsyncIterator[tuple[httpx.AsyncClient, Runtime, FakeGenerator]]:
    settings.ai = AISettings(model="test:fake", daily_token_budget=10_000)
    generator = FakeGenerator()
    runtime = Runtime.create(settings, channels={"push": push}, generator=generator)
    try:
        async with runtime.transaction() as session:
            _, raw = await api_keys.create(session, "smart", [Scope.ADMIN])
        app = create_app(runtime=runtime)
        app.state.runtime = runtime
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://cue.test",
            headers={"Authorization": f"Bearer {raw}"},
        ) as client:
            yield client, runtime, generator
    finally:
        await runtime.aclose()


async def test_smart_digest_shares_only_summary_fields_with_the_model(
    smart: tuple[httpx.AsyncClient, Runtime, FakeGenerator], push: FakeChannel
) -> None:
    client, runtime, generator = smart
    await _setup(client, summarize=True, summary_fields=["author"], send_after=2)
    await _comment(client, "Ann")
    await _comment(client, "Bo")
    await drain(runtime)

    [request] = generator.requests
    assert dict(request.context) == {
        "grouped_updates": [{"author": "Ann"}, {"author": "Bo"}],
        "update_count": 2,
    }
    assert "grouped" in request.instructions.lower()
    assert request.baseline.title == "2 new comments"
    [delivered] = push.sent
    assert delivered.content.body == "there, Ann, Bo commented on Post 1"
    assert delivered.content.thread == "post-1"
