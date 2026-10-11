from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select, update

from cue_notify.api.app import create_app
from cue_notify.db.models import ApiKey, Recipient
from cue_notify.runtime import Runtime
from cue_notify.services.events import pattern_matches
from cue_notify.settings import (
    ApiSettings,
    DatabaseSettings,
    PolicySettings,
    Settings,
    WorkerSettings,
)
from tests.helpers import TEST_SECRET_KEY, create_recipient, create_rule, create_template, drain

BASE = "https://notify.test"
FAKE_REVIEW_PATH = "/review/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        environment="test",
        database=DatabaseSettings(url=database_url, pool_size=5),
        worker=WorkerSettings(concurrency=4, retry_base_seconds=1, retry_max_seconds=2),
        policy=PolicySettings(default_locale="en", default_timezone="UTC", quiet_hours=None),
        api=ApiSettings(public_url=f"{BASE}/", secret_key=TEST_SECRET_KEY),
    )


def connect(runtime: Runtime, key: str | None = None) -> httpx.AsyncClient:
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cue.test", headers=headers
    )


async def issue_key(
    client: httpx.AsyncClient, name: str, scopes: list[str], agent: str | None = None
) -> httpx.Response:
    body: dict[str, Any] = {"name": name, "scopes": scopes}
    if agent is not None:
        body["agent"] = agent
    return await client.post("/v1/api-keys", json=body)


async def key_for(client: httpx.AsyncClient, name: str, scopes: list[str]) -> str:
    response = await issue_key(client, name, scopes)
    assert response.status_code == 201, response.text
    return response.json()["key"]


@pytest.fixture
async def anon(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    async with connect(runtime) as http:
        yield http


async def held_message(
    client: httpx.AsyncClient,
    runtime: Runtime,
    reviewers: list[str] | None = None,
    note: str = "hello",
) -> dict[str, Any]:
    existing = await client.get("/v1/agents/sales-bot")
    if existing.status_code == 404:
        await create_template(
            client, key="note", locales={"en": {"title": "Note", "body": "{{ data.note }}"}}
        )
        await create_recipient(client)
        body = {
            "key": "sales-bot",
            "name": "Sales assistant",
            "approval": True,
            "reviewers": reviewers or [],
        }
        assert (await client.post("/v1/agents", json=body)).status_code == 201
    response = await issue_key(
        client,
        f"sales-key-{hashlib.sha256(note.encode()).hexdigest()[:8]}",
        ["messages:write"],
        agent="sales-bot",
    )
    assert response.status_code == 201, response.text
    async with connect(runtime, response.json()["key"]) as agent:
        sent = await agent.post(
            "/v1/messages",
            json={
                "recipient": "user-1",
                "template": "note",
                "channels": ["push"],
                "data": {"note": note},
            },
        )
    assert sent.status_code == 202, sent.text
    assert sent.json()["status"] == "pending_approval"
    return sent.json()


async def relay_reviews(client: httpx.AsyncClient, **rule: Any) -> None:
    await create_template(
        client, key="relay", locales={"en": {"title": "Review", "body": "{{ data.review_url }}"}}
    )
    await create_recipient(client, "lead")
    await create_rule(
        client,
        **{
            "key": "tell-reviewers",
            "event": "cue.approval_requested",
            "template": "relay",
            "channels": ["push"],
            **rule,
        },
    )


async def test_every_message_response_hides_review_links_from_non_admin_keys(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await relay_reviews(client)
    await held_message(client, runtime, reviewers=["lead"])
    await drain(runtime)
    [notice] = (await client.get("/v1/messages", params={"recipient": "lead"})).json()["items"]
    raw = notice["content"]["default"]["body"]
    assert raw.startswith(f"{BASE}/review/")

    producer = await key_for(client, "app-backend", ["events:write"])
    async with connect(runtime, producer) as backend:
        reported = await backend.post(
            f"/v1/messages/{notice['id']}/engagement", json={"type": "opened"}
        )
    assert reported.status_code == 200, reported.text
    assert reported.json()["content"]["default"]["body"] == f"{BASE}/review/[redacted]"
    assert raw not in reported.text


async def test_decisions_return_redacted_content(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    note = f"see {BASE}{FAKE_REVIEW_PATH}"
    first = await held_message(client, runtime, note=note)
    second = await held_message(client, runtime, note=f"{note} again")
    decider = await key_for(client, "ops-desk", ["approvals:decide"])
    async with connect(runtime, decider) as desk:
        approved = await desk.post(f"/v1/messages/{first['id']}/approve")
        rejected = await desk.post(f"/v1/messages/{second['id']}/reject", json={"reason": "no"})
    assert approved.status_code == 200, approved.text
    assert rejected.status_code == 200, rejected.text
    for response in (approved, rejected):
        assert FAKE_REVIEW_PATH not in response.text
        assert "/review/[redacted]" in response.json()["content"]["default"]["body"]


async def test_digests_hide_review_links_and_need_the_exact_event_name(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await relay_reviews(client, digest={"window": "PT10M"})
    await create_rule(client, key="everything", event="*", template="relay", channels=["push"])
    await create_rule(client, key="all-of-cue", event="cue.*", template="relay", channels=["push"])
    await held_message(client, runtime, reviewers=["lead"])
    await drain(runtime)

    [event] = (await client.get("/v1/events", params={"name": "cue.approval_requested"})).json()[
        "items"
    ]
    assert [outcome["rule"] for outcome in event["outcome"]] == ["tell-reviewers"]

    [digest] = (await client.get("/v1/digests")).json()["items"]
    raw = digest["items"][0]["data"]["review_url"]
    assert raw.startswith(f"{BASE}/review/") and "[redacted]" not in raw

    reader = await key_for(client, "dashboard", ["messages:read"])
    async with connect(runtime, reader) as dashboard:
        listed = await dashboard.get("/v1/digests")
        fetched = await dashboard.get(f"/v1/digests/{digest['id']}")
    for response in (listed, fetched):
        assert response.status_code == 200, response.text
        assert raw not in response.text
        assert "/review/[redacted]" in response.text


def test_wildcards_never_match_cue_events() -> None:
    assert not pattern_matches("*", "cue.approval_requested")
    assert not pattern_matches("cue.*", "cue.approval_requested")
    assert not pattern_matches("cue.approval_*", "cue.approval_requested")
    assert pattern_matches("cue.approval_requested", "cue.approval_requested")
    assert pattern_matches("order.*", "order.shipped")
    assert pattern_matches("*", "order.shipped")


async def test_only_approvals_decide_keys_review_held_messages(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    first = await held_message(client, runtime, note="one")
    second = await held_message(client, runtime, note="two")
    writer = await key_for(client, "backend", ["messages:write", "messages:read"])
    async with connect(runtime, writer) as backend:
        assert (await backend.post(f"/v1/messages/{first['id']}/approve")).status_code == 403
        refused = await backend.post(f"/v1/messages/{first['id']}/reject", json={"reason": "x"})
        assert refused.status_code == 403
        assert "approvals:decide" in refused.json()["detail"]
    still = (await client.get(f"/v1/messages/{first['id']}")).json()
    assert still["status"] == "pending_approval"

    decider = await key_for(client, "ops-desk", ["approvals:decide"])
    async with connect(runtime, decider) as desk:
        approved = await desk.post(f"/v1/messages/{first['id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "queued"
    assert approved.json()["reviewed_by"] == "ops-desk"

    by_admin = await client.post(f"/v1/messages/{second['id']}/approve")
    assert by_admin.status_code == 200


async def test_only_approvals_decide_keys_decide_requests(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await create_recipient(client, "lead")
    asked = await client.post("/v1/approvals", json={"title": "Refund?", "reviewers": ["lead"]})
    assert asked.status_code == 201, asked.text
    approval_id = asked.json()["id"]

    writer = await key_for(client, "backend", ["messages:write", "messages:read"])
    async with connect(runtime, writer) as backend:
        assert (await backend.post(f"/v1/approvals/{approval_id}/approve")).status_code == 403
        assert (await backend.post(f"/v1/approvals/{approval_id}/reject")).status_code == 403
        assert (await backend.get(f"/v1/approvals/{approval_id}")).json()["status"] == "pending"

    decider = await key_for(client, "ops-desk", ["approvals:decide"])
    async with connect(runtime, decider) as desk:
        decided = await desk.post(f"/v1/approvals/{approval_id}/approve")
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"


async def test_review_links_still_let_people_decide(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, runtime: Runtime
) -> None:
    held = await held_message(client, runtime, reviewers=["lead"])
    await drain(runtime)
    [event] = (await client.get("/v1/events", params={"name": "cue.approval_requested"})).json()[
        "items"
    ]
    path = event["data"]["review_url"].removeprefix(BASE)
    response = await anon.post(path, data={"action": "approve"})
    assert response.status_code == 200, response.text
    assert (await client.get(f"/v1/messages/{held['id']}")).json()["status"] == "queued"


@pytest.mark.parametrize(
    "scope", ["recipients:read", "recipients:write", "approvals:decide", "admin"]
)
async def test_agent_keys_cannot_hold_people_or_decision_scopes(
    client: httpx.AsyncClient, scope: str
) -> None:
    body = {"key": "sales-bot", "name": "Sales assistant"}
    assert (await client.post("/v1/agents", json=body)).status_code == 201
    response = await issue_key(client, "bot", ["messages:write", scope], agent="sales-bot")
    assert response.status_code == 422
    assert scope in response.json()["detail"]


async def test_stored_agent_keys_with_forbidden_scopes_are_denied(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    held = await held_message(client, runtime)
    await create_recipient(client, "lead")
    asked = await client.post("/v1/approvals", json={"title": "Refund?", "reviewers": ["lead"]})
    response = await issue_key(client, "legacy-bot", ["messages:read"], agent="sales-bot")
    assert response.status_code == 201
    forbidden = ["messages:read", "recipients:read", "recipients:write", "approvals:decide"]
    async with runtime.transaction() as session:
        await session.execute(
            update(ApiKey)
            .where(ApiKey.id == UUID(response.json()["id"]))
            .values(scopes=[*forbidden, "admin"])
        )
    async with connect(runtime, response.json()["key"]) as legacy:
        assert (await legacy.get("/v1/messages")).status_code == 200
        denied = [
            await legacy.get("/v1/recipients"),
            await legacy.get("/v1/recipients/user-1"),
            await legacy.put("/v1/recipients/user-1", json={"locale": "uz"}),
            await legacy.post(f"/v1/messages/{held['id']}/approve"),
            await legacy.post(f"/v1/approvals/{asked.json()['id']}/approve"),
            await legacy.get("/v1/api-keys"),
        ]
    assert [r.status_code for r in denied] == [403] * len(denied)


async def test_event_producers_cannot_rewrite_profiles(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await create_recipient(client, "victim", locale="fr", timezone="Europe/Paris")
    producer = await key_for(client, "producer", ["events:write", "messages:write"])

    async def ingest(http: httpx.AsyncClient, recipient: dict[str, Any]) -> int:
        response = await http.post("/v1/events", json={"name": "ping", "recipient": recipient})
        return response.status_code

    async with connect(runtime, producer) as backend:
        refused = [
            {"external_id": "victim", "region": "US-FL"},
            {"external_id": "victim", "attributes": {"vip": False}},
            {"external_id": "victim", "timezone": "America/New_York"},
            {"external_id": "victim", "locale": "en"},
            {"external_id": "newcomer", "region": "US"},
            {"external_id": "newcomer", "attributes": {"plan": "pro"}},
        ]
        for recipient in refused:
            assert await ingest(backend, recipient) == 403, recipient
        send = await backend.post(
            "/v1/messages",
            json={
                "recipient": {"external_id": "victim", "timezone": "Asia/Tokyo"},
                "template": "missing",
                "channels": ["push"],
            },
        )
        assert send.status_code == 403

        unchanged = {"external_id": "victim", "locale": "fr", "timezone": "Europe/Paris"}
        assert await ingest(backend, unchanged) == 202
        created = {"external_id": "newcomer", "locale": "uz", "timezone": "Asia/Tashkent"}
        assert await ingest(backend, created) == 202
        assert await ingest(backend, {"external_id": "bare"}) == 202
        filled = {"external_id": "bare", "timezone": "Asia/Tashkent"}
        assert await ingest(backend, filled) == 202

    victim = (await client.get("/v1/recipients/victim")).json()
    assert (victim["locale"], victim["timezone"], victim["region"]) == ("fr", "Europe/Paris", None)
    assert victim["attributes"] == {}
    newcomer = (await client.get("/v1/recipients/newcomer")).json()
    assert (newcomer["locale"], newcomer["timezone"]) == ("uz", "Asia/Tashkent")
    assert (await client.get("/v1/recipients/bare")).json()["timezone"] == "Asia/Tashkent"

    owner = await key_for(client, "crm", ["events:write", "recipients:write"])
    async with connect(runtime, owner) as crm:
        moved = {"external_id": "victim", "timezone": "Asia/Tokyo", "region": "JP"}
        assert await ingest(crm, moved) == 202
    victim = (await client.get("/v1/recipients/victim")).json()
    assert (victim["timezone"], victim["region"]) == ("Asia/Tokyo", "JP")


async def test_preference_tokens_are_never_stored(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, runtime: Runtime
) -> None:
    await create_recipient(client)
    token = (await client.get("/v1/recipients/user-1")).json()["preference_token"]
    assert token
    async with runtime.session() as session:
        row = (
            (
                await session.execute(
                    select(Recipient.__table__).where(Recipient.external_id == "user-1")
                )
            )
            .mappings()
            .one()
        )
    assert all(token not in str(value) for value in row.values())
    assert row["preference_token_hash"] is None

    assert (await anon.get(f"/v1/preferences/{token}")).status_code == 200
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert (await anon.get(f"/v1/preferences/{tampered}")).status_code == 404


async def test_links_sent_before_the_upgrade_keep_working_until_rotated(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, runtime: Runtime
) -> None:
    await create_recipient(client)
    legacy = "legacy-token-from-an-old-email-0"
    async with runtime.transaction() as session:
        await session.execute(
            update(Recipient)
            .where(Recipient.external_id == "user-1")
            .values(preference_token_hash=hashlib.sha256(legacy.encode()).hexdigest())
        )
    current = (await client.get("/v1/recipients/user-1")).json()["preference_token"]

    assert (await anon.get(f"/v1/preferences/{legacy}")).status_code == 200
    unsubscribed = await anon.post(
        f"/preferences/{legacy}/unsubscribe?category=marketing",
        content="List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert unsubscribed.status_code == 200
    profile = (await client.get("/v1/recipients/user-1")).json()
    assert profile["preferences"]["unsubscribed_categories"] == ["marketing"]

    rotated = (await client.post("/v1/recipients/user-1/preference-token")).json()
    fresh = rotated["preference_token"]
    assert fresh not in (current, legacy)
    assert (await anon.get(f"/v1/preferences/{legacy}")).status_code == 404
    assert (await anon.get(f"/v1/preferences/{current}")).status_code == 404
    assert (await anon.get(f"/v1/preferences/{fresh}")).status_code == 200


async def test_links_in_messages_use_the_signed_token(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, runtime: Runtime
) -> None:
    await create_template(
        client,
        key="promo",
        locales={"en": {"body": "Stop: {{ links.unsubscribe }}"}},
    )
    await create_recipient(client)
    sent = await client.post(
        "/v1/messages",
        json={
            "recipient": "user-1",
            "template": "promo",
            "category": "marketing",
            "channels": ["push"],
        },
    )
    assert sent.status_code == 202, sent.text
    link = sent.json()["content"]["default"]["body"].removeprefix("Stop: ")
    assert link.startswith(f"{BASE}/preferences/")
    response = await anon.post(
        link.removeprefix(BASE),
        content="List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200


def test_public_url_needs_a_long_enough_secret_key() -> None:
    with pytest.raises(ValidationError, match="secret_key is required"):
        ApiSettings(public_url=BASE)
    with pytest.raises(ValidationError, match="at least 32 characters"):
        ApiSettings(public_url=BASE, secret_key=SecretStr("short"))
    assert ApiSettings(public_url=BASE, secret_key=TEST_SECRET_KEY).public_url == BASE
