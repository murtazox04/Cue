"""Preference centre, one-click unsubscribe and the consent ledger."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from cue_notify.api.app import create_app
from cue_notify.runtime import Runtime
from cue_notify.settings import (
    ApiSettings,
    DatabaseSettings,
    PolicySettings,
    Settings,
    WorkerSettings,
)
from tests.helpers import FakeChannel, create_recipient, create_rule, create_template, drain

BASE = "https://notify.test"


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        environment="test",
        database=DatabaseSettings(url=database_url, pool_size=5),
        worker=WorkerSettings(concurrency=4, retry_base_seconds=1, retry_max_seconds=2),
        policy=PolicySettings(default_locale="en", default_timezone="UTC", quiet_hours=None),
        api=ApiSettings(public_url=f"{BASE}/", product_name="Shop"),
    )


@pytest.fixture
async def anon(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    """A client with no API key, like a person's browser or mail client."""
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cue.test"
    ) as http:
        yield http


@pytest.fixture
async def token(client: httpx.AsyncClient) -> str:
    recipient = await create_recipient(client)
    return recipient["preference_token"]


async def _history(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await client.get("/v1/recipients/user-1/preferences/history")
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _prefs(client: httpx.AsyncClient) -> dict[str, Any]:
    return (await client.get("/v1/recipients/user-1")).json()["preferences"]


# --------------------------------------------------------------------------- links


async def test_messages_carry_preference_and_unsubscribe_links(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, token: str
) -> None:
    await create_template(
        client,
        key="promo",
        locales={
            "en": {
                "title": "Sale",
                "body": "Manage: {{ links.preferences }} | Leave: {{ links.unsubscribe }}",
            }
        },
    )
    await create_rule(client, template="promo", category="marketing", channels=["push"])
    await client.post(
        "/v1/events", json={"name": "order.shipped", "recipient": "user-1", "data": {}}
    )
    await drain(runtime)

    page = f"{BASE}/preferences/{token}"
    unsubscribe = f"{page}/unsubscribe?category=marketing"
    [delivery] = push.sent
    assert delivery.content.body == f"Manage: {page} | Leave: {unsubscribe}"
    assert delivery.links == {"preferences": page, "unsubscribe": unsubscribe}


async def test_transactional_messages_get_no_unsubscribe_link(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, token: str
) -> None:
    await create_template(
        client,
        locales={"en": {"title": "Shipped", "body": "{{ links.unsubscribe or 'none' }}"}},
    )
    await create_rule(client, channels=["push"])
    await client.post(
        "/v1/events", json={"name": "order.shipped", "recipient": "user-1", "data": {}}
    )
    await drain(runtime)
    [delivery] = push.sent
    assert delivery.content.body == "none"
    assert delivery.links == {"preferences": f"{BASE}/preferences/{token}"}


# --------------------------------------------------------------------------- JSON API


async def test_preference_api_needs_only_the_token(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, token: str
) -> None:
    response = await anon.get(f"/v1/preferences/{token}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["categories"] == [
        {
            "key": "marketing",
            "description": body["categories"][0]["description"],
            "subscribed": True,
            "can_unsubscribe": True,
        },
        {
            "key": "transactional",
            "description": body["categories"][1]["description"],
            "subscribed": True,
            "can_unsubscribe": False,
        },
    ]
    assert body["channels"] == [{"name": "push", "muted": False}, {"name": "sms", "muted": False}]

    response = await anon.put(
        f"/v1/preferences/{token}",
        json={"unsubscribed_categories": ["marketing"], "muted_channels": ["sms"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["categories"][0]["subscribed"] is False
    assert response.json()["channels"][1] == {"name": "sms", "muted": True}

    [entry] = await _history(client)
    assert entry["source"] == "self_service"
    assert entry["changes"] == {"unsubscribed": ["marketing"], "muted": ["sms"]}


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"unsubscribed_categories": ["transactional"]}, "cannot unsubscribe"),
        ({"unsubscribed_categories": ["nope"]}, "unknown categories"),
        ({"muted_channels": ["pigeon"]}, "unknown channels"),
    ],
)
async def test_preference_api_rejects_invalid_choices(
    anon: httpx.AsyncClient, token: str, body: dict[str, Any], fragment: str
) -> None:
    response = await anon.put(f"/v1/preferences/{token}", json=body)
    assert response.status_code == 422
    assert fragment in response.json()["detail"]


async def test_unknown_token_is_404(anon: httpx.AsyncClient) -> None:
    assert (await anon.get("/v1/preferences/not-a-token")).status_code == 404


async def test_api_changes_are_recorded_too_and_no_ops_are_not(
    client: httpx.AsyncClient, token: str
) -> None:
    body = {"unsubscribed_categories": ["marketing"]}
    await client.put("/v1/recipients/user-1/preferences", json=body)
    await client.put("/v1/recipients/user-1/preferences", json=body)
    [entry] = await _history(client)
    assert entry["source"] == "api"
    assert entry["changes"] == {"unsubscribed": ["marketing"]}


async def test_rotating_the_token_invalidates_old_links(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, token: str
) -> None:
    response = await client.post("/v1/recipients/user-1/preference-token")
    assert response.status_code == 200, response.text
    fresh = response.json()["preference_token"]
    assert fresh != token
    assert (await anon.get(f"/v1/preferences/{token}")).status_code == 404
    assert (await anon.get(f"/v1/preferences/{fresh}")).status_code == 200
    missing = await client.post("/v1/recipients/nobody/preference-token")
    assert missing.status_code == 404


# --------------------------------------------------------------------------- one-click


async def test_one_click_unsubscribe(
    client: httpx.AsyncClient,
    anon: httpx.AsyncClient,
    runtime: Runtime,
    push: FakeChannel,
    token: str,
) -> None:
    url = f"/preferences/{token}/unsubscribe?category=marketing"

    # Mail scanners prefetch links: GET only asks for confirmation.
    response = await anon.get(url)
    assert response.status_code == 200
    assert "<button>Unsubscribe</button>" in response.text
    assert await _history(client) == []

    response = await anon.post(
        url,
        content="List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    assert "You are unsubscribed from Marketing messages" in response.text
    assert (await _prefs(client))["unsubscribed_categories"] == ["marketing"]
    [entry] = await _history(client)
    assert entry["source"] == "one_click"

    await create_template(client)
    await create_rule(client, category="marketing", channels=["push"])
    await client.post("/v1/events", json={"name": "order.shipped", "recipient": "user-1"})
    await drain(runtime)
    assert push.sent == []


async def test_one_click_cannot_leave_transactional(anon: httpx.AsyncClient, token: str) -> None:
    response = await anon.post(f"/preferences/{token}/unsubscribe?category=transactional")
    assert response.status_code == 422
    assert "cannot unsubscribe" in response.text


# --------------------------------------------------------------------------- hosted page


async def test_hosted_page_renders_safely(anon: httpx.AsyncClient, token: str) -> None:
    response = await anon.get(f"/preferences/{token}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    html = response.text
    assert "<title>Shop notification preferences</title>" in html
    assert 'value="marketing" checked>' in html
    assert 'value="transactional" checked disabled>' in html
    assert "Text messages (SMS)" in html
    assert "<script" not in html


async def test_hosted_page_saves_the_form(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, token: str
) -> None:
    # Only "push" left ticked; marketing unticked. Disabled boxes are never submitted.
    response = await anon.post(
        f"/preferences/{token}",
        content="channel=push&action=save",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "?saved=true"
    prefs = await _prefs(client)
    assert prefs["unsubscribed_categories"] == ["marketing"]
    assert prefs["muted_channels"] == ["sms"]

    saved = await anon.get(f"/preferences/{token}?saved=true")
    assert "Your preferences are saved." in saved.text
    assert 'value="marketing">' in saved.text


async def test_hosted_page_keeps_choices_it_does_not_show(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, token: str
) -> None:
    # Set through the API on a route this instance does not offer: not on the page.
    await client.put("/v1/recipients/user-1/preferences", json={"muted_channels": ["telegram"]})
    page = await anon.get(f"/preferences/{token}")
    assert 'value="telegram"' not in page.text
    await anon.post(
        f"/preferences/{token}",
        content="category=marketing&channel=push&action=save",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert (await _prefs(client))["muted_channels"] == ["sms", "telegram"]


async def test_hosted_page_unsubscribe_all(
    client: httpx.AsyncClient, anon: httpx.AsyncClient, token: str
) -> None:
    await anon.post(
        f"/preferences/{token}",
        content="action=unsubscribe_all",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert (await _prefs(client))["unsubscribed_categories"] == ["marketing"]


async def test_hosted_page_with_a_stale_link(anon: httpx.AsyncClient) -> None:
    response = await anon.get("/preferences/stale")
    assert response.status_code == 404
    assert "This link is no longer valid" in response.text


async def test_hosted_form_size_is_bounded(anon: httpx.AsyncClient, token: str) -> None:
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    huge = await anon.post(f"/preferences/{token}", content="channel=" + "x" * 20_000, headers=form)
    assert huge.status_code == 400
    assert "too large" in huge.text
    many = await anon.post(
        f"/preferences/{token}", content="&".join(["category=a"] * 300), headers=form
    )
    assert many.status_code == 400


async def test_confirmation_page_only_names_real_categories(
    anon: httpx.AsyncClient, token: str
) -> None:
    spoof = await anon.get(f"/preferences/{token}/unsubscribe?category=Call%20this%20number")
    assert spoof.status_code == 404
    assert "Call this number" not in spoof.text
    locked = await anon.get(f"/preferences/{token}/unsubscribe?category=transactional")
    assert locked.status_code == 422
