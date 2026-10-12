"""US texting rules for marketing, STOP replies, and proof of consent in the ledger."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from cue_notify.api.app import create_app
from cue_notify.api.routers.inbound import twilio_signature
from cue_notify.channels.twilio import TwilioChannel, TwilioConfig
from cue_notify.runtime import Runtime
from cue_notify.settings import (
    ApiSettings,
    ComplianceSettings,
    DatabaseSettings,
    PolicySettings,
    Settings,
    WorkerSettings,
)
from tests.helpers import TEST_SECRET_KEY, FakeChannel, create_recipient, create_template

BASE = "https://notify.test"
PHONE = "+15551230000"


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        environment="test",
        database=DatabaseSettings(url=database_url, pool_size=5),
        worker=WorkerSettings(concurrency=4, retry_base_seconds=1, retry_max_seconds=2),
        policy=PolicySettings(default_locale="en", default_timezone="UTC", quiet_hours=None),
        api=ApiSettings(public_url=f"{BASE}/", secret_key=TEST_SECRET_KEY),
        compliance=ComplianceSettings(us_texting_rules=True),
    )


async def _shopper(client: httpx.AsyncClient, **fields: Any) -> None:
    await create_template(client)
    await create_recipient(
        client,
        external_id="shopper",
        timezone="America/New_York",
        addresses=[{"channel": "sms", "value": PHONE}, {"channel": "push", "value": "tok"}],
        **fields,
    )


async def _promo(client: httpx.AsyncClient, send_at: datetime, **fields: Any) -> dict[str, Any]:
    body = {
        "recipient": "shopper",
        "template": "order-shipped",
        "channels": ["sms"],
        "category": "marketing",
        "data": {"order_id": "1"},
        "send_at": send_at.isoformat(),
        **fields,
    }
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 202, response.text
    return response.json()


async def test_marketing_texts_wait_for_legal_hours(client: httpx.AsyncClient) -> None:
    await _shopper(client, region="US-FL")
    late = datetime(2026, 12, 2, 4, 0, tzinfo=UTC)  # 23:00 Tuesday in New York
    message = await _promo(client, late)
    assert message["status"] == "queued"
    assert message["send_at"].startswith("2026-12-02T13:00")  # 08:00 local

    # Transactional texts are not marketing: they go when asked.
    receipt = await _promo(client, late, category="transactional")
    assert receipt["send_at"].startswith("2026-12-02T04:00")
    # Neither are marketing pushes.
    push = await _promo(client, late, channels=["push"])
    assert push["send_at"].startswith("2026-12-02T04:00")


async def test_states_cap_marketing_texts_at_three_a_day(client: httpx.AsyncClient) -> None:
    await _shopper(client, region="US-FL")
    # A marketing category of your own, without Cue's default caps.
    deals = {"key": "deals", "marketing": True}
    assert (await client.post("/v1/categories", json=deals)).status_code == 201
    noon = datetime(2026, 12, 2, 17, 0, tzinfo=UTC)
    statuses = [(await _promo(client, noon, category="deals"))["status"] for _ in range(4)]
    assert statuses == ["queued", "queued", "queued", "suppressed"]
    capped = (await client.get("/v1/messages", params={"status": "suppressed"})).json()["items"]
    assert capped[0]["status_reason"] == (
        "compliance: 3 marketing texts per 24 hours (Fla. Stat. 501.616(6))"
    )


async def test_recipients_outside_the_us_are_unaffected(client: httpx.AsyncClient) -> None:
    await _shopper(client, region="DE")
    late = datetime(2026, 12, 2, 4, 0, tzinfo=UTC)
    assert (await _promo(client, late))["send_at"].startswith("2026-12-02T04:00")


async def test_stop_mutes_texts_and_start_brings_them_back(client: httpx.AsyncClient) -> None:
    await _shopper(client)
    stop = await client.post("/v1/inbound/sms", json={"from": PHONE, "body": " Stop. "})
    assert stop.json() == {"action": "opt_out", "recipients": ["shopper"]}
    shopper = (await client.get("/v1/recipients/shopper")).json()
    assert shopper["preferences"]["muted_channels"] == ["sms"]
    assert "marketing" in shopper["preferences"]["unsubscribed_categories"]

    noon = datetime(2026, 12, 2, 17, 0, tzinfo=UTC)
    blocked = await _promo(client, noon, category="transactional")
    assert blocked["status_reason"] == "no_reachable_channel"

    other = await client.post("/v1/inbound/sms", json={"from": PHONE, "body": "thanks!"})
    assert other.json() == {"action": None, "recipients": []}
    start = await client.post("/v1/inbound/sms", json={"from": PHONE, "body": "START"})
    assert start.json()["action"] == "opt_in"
    shopper = (await client.get("/v1/recipients/shopper")).json()
    assert shopper["preferences"]["muted_channels"] == []
    # Marketing needs a fresh opt-in; START only restores texting.
    assert "marketing" in shopper["preferences"]["unsubscribed_categories"]

    history = (await client.get("/v1/recipients/shopper/preferences/history")).json()["items"]
    assert {h["source"] for h in history} == {"sms_keyword"}
    assert {h["provenance"]["keyword"] for h in history} == {"Stop.", "START"}
    assert history[0]["provenance"]["api_key"] == "tests"


async def test_the_hosted_page_records_how_consent_changed(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await _shopper(client)
    token = (await client.get("/v1/recipients/shopper")).json()["preference_token"]
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 1234)),
        base_url="http://cue.test",
        headers={"User-Agent": "Mail/1.0"},
    ) as browser:
        done = await browser.post(f"/preferences/{token}/unsubscribe?category=marketing")
    assert done.status_code == 200
    [change] = (await client.get("/v1/recipients/shopper/preferences/history")).json()["items"]
    assert change["provenance"] == {
        "method": "one_click",
        "ip": "203.0.113.9",
        "user_agent": "Mail/1.0",
    }


@pytest.fixture
async def twilio(settings: Settings, push: FakeChannel) -> AsyncIterator[Runtime]:
    http = httpx.AsyncClient()
    config = TwilioConfig(account_sid="AC1", auth_token="twilio-token", from_number="+15550000000")
    rt = Runtime.create(
        settings, channels={"push": push, "sms": TwilioChannel("sms", config, http)}
    )
    try:
        yield rt
    finally:
        await rt.aclose()
        await http.aclose()


def _reference_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's documented algorithm, written independently of Cue's implementation."""
    data = url + "".join(name + params[name] for name in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


async def test_twilio_replies_are_verified(
    client: httpx.AsyncClient, twilio: Runtime, runtime: Runtime
) -> None:
    await _shopper(client)
    app = create_app(runtime=twilio)
    app.state.runtime = twilio
    # Twilio sends (and signs) empty parameters too.
    form = {"From": PHONE, "Body": "UNSUBSCRIBE", "MessageSid": "SM1", "FromCity": ""}
    url = f"{BASE}/v1/inbound/twilio"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cue.test"
    ) as twilio_server:
        forged = await twilio_server.post(
            "/v1/inbound/twilio", data=form, headers={"X-Twilio-Signature": "nope"}
        )
        assert forged.status_code == 403
        junk = await twilio_server.post(
            "/v1/inbound/twilio", data=form, headers={"X-Twilio-Signature": b"\xe9t\xe9"}
        )
        assert junk.status_code == 403
        assert twilio_signature("twilio-token", url, {k: [v] for k, v in form.items()}) == (
            _reference_signature("twilio-token", url, form)
        )
        signature = _reference_signature("twilio-token", url, form)
        ok = await twilio_server.post(
            "/v1/inbound/twilio", data=form, headers={"X-Twilio-Signature": signature}
        )
    assert ok.status_code == 200
    assert ok.text.endswith("<Response/>")
    shopper = (await client.get("/v1/recipients/shopper")).json()
    assert shopper["preferences"]["muted_channels"] == ["sms"]


async def test_opt_ins_keep_the_wording_and_expire(client: httpx.AsyncClient) -> None:
    await _shopper(client)
    await client.put(
        "/v1/recipients/shopper/preferences", json={"unsubscribed_categories": ["marketing"]}
    )
    disclosure = "Yes, text me deals from Shop. Msg & data rates may apply. Reply STOP to end."
    consent = {
        "categories": ["marketing"],
        "disclosure": disclosure,
        "disclosure_version": "2026-10",
        "method": "checkbox",
        "source": "https://shop.example/checkout",
        "ip": "198.51.100.7",
        "expires_at": "2027-10-11T00:00:00Z",
    }
    response = await client.post("/v1/recipients/shopper/consent", json=consent)
    assert response.status_code == 201, response.text
    change = response.json()
    assert change["changes"]["consented"] == ["marketing"]
    assert change["changes"]["resubscribed"] == ["marketing"]
    assert change["provenance"]["disclosure"] == disclosure
    assert len(change["provenance"]["disclosure_sha256"]) == 64
    assert change["provenance"]["api_key"] == "tests"

    # Messages go while the opt-in holds, and stop once it has expired.
    noon = datetime(2026, 12, 2, 17, 0, tzinfo=UTC)
    assert (await _promo(client, noon, channels=["push"]))["status"] == "queued"
    expired = {**consent, "expires_at": "2026-01-01T00:00:00Z"}
    assert (await client.post("/v1/recipients/shopper/consent", json=expired)).status_code == 201
    late = await _promo(client, noon, channels=["push"])
    assert (late["status"], late["status_reason"]) == ("suppressed", "consent_expired")

    unknown = await client.post(
        "/v1/recipients/shopper/consent", json={**consent, "categories": ["nope"]}
    )
    assert unknown.status_code == 422
