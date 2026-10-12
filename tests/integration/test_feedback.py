"""Delivery feedback: bounces, soft bounces, complaints, deliveries and channel health."""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from cue_notify.api.app import create_app
from cue_notify.channels.twilio import TwilioChannel, TwilioConfig
from cue_notify.runtime import Runtime
from cue_notify.settings import (
    ApiSettings,
    DatabaseSettings,
    DeliverySettings,
    PolicySettings,
    Settings,
    WorkerSettings,
)
from tests.helpers import TEST_SECRET_KEY, FakeChannel, create_recipient, create_template, drain

BASE = "https://notify.test"
PHONE = "+15551230000"


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        environment="test",
        database=DatabaseSettings(url=database_url, pool_size=5),
        worker=WorkerSettings(concurrency=4, retry_base_seconds=30, retry_max_seconds=60),
        policy=PolicySettings(default_locale="en", default_timezone="UTC", quiet_hours=None),
        api=ApiSettings(public_url=f"{BASE}/", secret_key=TEST_SECRET_KEY),
        delivery=DeliverySettings(soft_bounce_limit=2, breaker_failures=2),
    )


async def _send(client: httpx.AsyncClient, channels: list[str], **fields: Any) -> dict[str, Any]:
    body = {
        "recipient": "user-1",
        "template": "order-shipped",
        "category": "transactional",
        "channels": channels,
        "data": {"order_id": "A-1"},
        **fields,
    }
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 202, response.text
    return response.json()


async def _setup(client: httpx.AsyncClient) -> None:
    await create_template(client)
    await create_recipient(client)


async def _addresses(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    recipient = (await client.get("/v1/recipients/user-1")).json()
    return {a["value"]: a for a in recipient["addresses"]}


async def test_a_bounce_disables_the_address_and_fails_the_message(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await _setup(client)
    sent = await _send(client, ["push"])
    await drain(runtime)
    message = (await client.get(f"/v1/messages/{sent['id']}")).json()
    report = {
        "type": "bounce",
        "channel": "push",
        "provider_message_id": message["provider_message_id"],
        "reason": "unregistered",
    }
    result = await client.post("/v1/feedback", json=report)
    assert result.status_code == 200, result.text
    assert result.json() == {
        "message_id": sent["id"],
        "recipients": ["user-1"],
        "disabled": 1,
        "unsubscribed": False,
    }
    bounced = (await client.get(f"/v1/messages/{sent['id']}")).json()
    assert (bounced["status"], bounced["status_reason"]) == ("failed", "bounced: unregistered")
    address = (await _addresses(client))["token-1"]
    assert (address["enabled"], address["disabled_reason"]) == (False, "bounced: unregistered")

    # Repeating the report changes nothing, and nothing goes to the address any more.
    again = await client.post("/v1/feedback", json=report)
    assert again.json()["disabled"] == 0
    later = await _send(client, ["push"])
    await drain(runtime)
    unreachable = (await client.get(f"/v1/messages/{later['id']}")).json()
    assert (unreachable["status"], unreachable["status_reason"]) == (
        "suppressed",
        "no_reachable_channel",
    )
    assert len(push.sent) == 1


async def test_soft_bounces_in_a_row_disable_the_address(client: httpx.AsyncClient) -> None:
    await _setup(client)
    soft = {"type": "soft_bounce", "channel": "sms", "address": "+15550001"}
    await client.post("/v1/feedback", json=soft)
    # A delivery in between starts the count again.
    await client.post("/v1/feedback", json={**soft, "type": "delivered"})
    await client.post("/v1/feedback", json=soft)
    assert (await _addresses(client))["+15550001"]["enabled"] is True
    result = await client.post("/v1/feedback", json={**soft, "reason": "mailbox full"})
    assert result.json()["disabled"] == 1
    address = (await _addresses(client))["+15550001"]
    assert address["disabled_reason"] == "2 soft bounces in a row: mailbox full"


async def test_a_complaint_unsubscribes_from_optional_categories(
    client: httpx.AsyncClient,
) -> None:
    await _setup(client)
    report = {"type": "complaint", "channel": "sms", "address": "+15550001"}
    result = await client.post("/v1/feedback", json=report)
    assert result.json()["unsubscribed"] is True
    recipient = (await client.get("/v1/recipients/user-1")).json()
    assert "marketing" in recipient["preferences"]["unsubscribed_categories"]
    assert "transactional" not in recipient["preferences"]["unsubscribed_categories"]
    assert all(a["enabled"] for a in recipient["addresses"])  # receipts still arrive
    [change] = (await client.get("/v1/recipients/user-1/preferences/history")).json()["items"]
    assert change["source"] == "complaint"
    assert change["provenance"]["method"] == "complaint"
    assert change["provenance"]["address"] == "+15550001"


async def test_delivered_feedback_marks_the_message(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await _setup(client)
    sent = await _send(client, ["push"])
    await drain(runtime)
    result = await client.post(
        "/v1/feedback", json={"type": "delivered", "channel": "push", "message_id": sent["id"]}
    )
    assert result.status_code == 200
    message = (await client.get(f"/v1/messages/{sent['id']}")).json()
    assert message["status"] == "delivered"
    # Feedback naming another channel's message is not applied to it.
    other = await client.post(
        "/v1/feedback", json={"type": "bounce", "channel": "sms", "message_id": sent["id"]}
    )
    assert other.json()["message_id"] is None


async def test_feedback_needs_recipients_write_and_something_to_act_on(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    empty = await client.post("/v1/feedback", json={"type": "bounce", "channel": "sms"})
    assert empty.status_code == 422
    key = await client.post("/v1/api-keys", json={"name": "ops", "scopes": ["messages:read"]})
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://cue.test",
        headers={"Authorization": f"Bearer {key.json()['key']}"},
    ) as reader:
        report = {"type": "bounce", "channel": "sms", "address": "+15550001"}
        assert (await reader.post("/v1/feedback", json=report)).status_code == 403


async def test_failing_channels_are_routed_around(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, sms: FakeChannel
) -> None:
    await _setup(client)
    push.fail("token-1", retryable=True)
    first = await _send(client, ["push", "sms"])
    await drain(runtime)
    # One failure: the message waits to retry push, its first choice.
    assert (await client.get(f"/v1/messages/{first['id']}")).json()["status"] == "queued"
    second = await _send(client, ["push", "sms"])
    third = await _send(client, ["push", "sms"])
    await drain(runtime)
    # The second failure in a row opened the breaker: both go out over sms at once.
    for sent in (second, third):
        message = (await client.get(f"/v1/messages/{sent['id']}")).json()
        assert (message["status"], message["channel"]) == ("sent", "sms")
    assert not push.sent
    assert runtime.health.snapshot()["push"]["failures"] == 2

    # A message with push as its only route still tries it.
    only = await _send(client, ["push"])
    await drain(runtime)
    assert (await client.get(f"/v1/messages/{only['id']}")).json()["status"] == "queued"


# --------------------------------------------------------------------------- Twilio


def _signature(token: str, url: str, params: dict[str, str]) -> str:
    data = url + "".join(name + params[name] for name in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


@pytest.fixture
async def twilio(settings: Settings, push: FakeChannel) -> AsyncIterator[Runtime]:
    sids = iter(["SM1", "SM2"])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"sid": next(sids)})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = TwilioConfig(account_sid="AC1", auth_token="twilio-token", from_number="+15550000000")
    rt = Runtime.create(
        settings, channels={"push": push, "sms": TwilioChannel("sms", config, http)}, http=http
    )
    try:
        yield rt
    finally:
        await rt.aclose()


async def test_twilio_status_callbacks_are_feedback(
    client: httpx.AsyncClient, twilio: Runtime, runtime: Runtime
) -> None:
    await _setup(client)
    await create_recipient(client, "user-2", addresses=[{"channel": "sms", "value": PHONE}])
    first = await _send(client, ["sms"])
    second = await _send(client, ["sms"], recipient="user-2")
    await drain(twilio)

    app = create_app(runtime=twilio)
    app.state.runtime = twilio
    url = f"{BASE}/v1/inbound/twilio/status"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cue.test"
    ) as server:

        async def report(**params: str) -> httpx.Response:
            form = {"AccountSid": "AC1", "ErrorCode": "", **params}
            signature = _signature("twilio-token", url, form)
            return await server.post(
                "/v1/inbound/twilio/status", data=form, headers={"X-Twilio-Signature": signature}
            )

        delivered = await report(MessageSid="SM1", MessageStatus="delivered", To="+15550001")
        assert delivered.status_code == 204
        dead = await report(
            MessageSid="SM2", MessageStatus="undelivered", ErrorCode="30005", To=PHONE
        )
        assert dead.status_code == 204
        forged = await server.post(
            "/v1/inbound/twilio/status",
            data={"AccountSid": "AC1", "MessageSid": "SM1", "MessageStatus": "failed"},
            headers={"X-Twilio-Signature": "nope"},
        )
        assert forged.status_code == 403
        unknown = await server.post(
            "/v1/inbound/twilio/status",
            data={"AccountSid": "AC\u00e9", "MessageSid": "SM1"},
            headers={"X-Twilio-Signature": "nope"},
        )
        assert unknown.status_code == 404

    assert (await client.get(f"/v1/messages/{first['id']}")).json()["status"] == "delivered"
    failed = (await client.get(f"/v1/messages/{second['id']}")).json()
    assert (failed["status"], failed["status_reason"]) == (
        "failed",
        "bounced: twilio undelivered 30005",
    )
    user2 = (await client.get("/v1/recipients/user-2")).json()
    assert user2["addresses"][0]["enabled"] is False
