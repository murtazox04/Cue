from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from cue_notify.api.app import create_app
from cue_notify.channels.base import DeliveryError
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


async def send(client: httpx.AsyncClient, channels: list[str], **fields: Any) -> dict[str, Any]:
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


async def message_of(client: httpx.AsyncClient, sent: dict[str, Any]) -> dict[str, Any]:
    return (await client.get(f"/v1/messages/{sent['id']}")).json()


async def setup(client: httpx.AsyncClient, **fields: Any) -> None:
    await create_template(client)
    await create_recipient(client, **fields)


async def addresses_of(
    client: httpx.AsyncClient, external_id: str = "user-1"
) -> dict[str, dict[str, Any]]:
    recipient = (await client.get(f"/v1/recipients/{external_id}")).json()
    return {a["value"]: a for a in recipient["addresses"]}


def keyed_client(runtime: Runtime, key: str) -> httpx.AsyncClient:
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://cue.test",
        headers={"Authorization": f"Bearer {key}"},
    )


async def test_a_bounce_disables_the_address_and_fails_the_message(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await setup(client)
    sent = await send(client, ["push"])
    await drain(runtime)
    message = await message_of(client, sent)
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
        "recipient_count": 1,
        "disabled": 1,
        "unsubscribed": False,
        "duplicate": False,
    }
    bounced = await message_of(client, sent)
    assert (bounced["status"], bounced["status_reason"]) == ("failed", "bounced: unregistered")
    address = (await addresses_of(client))["token-1"]
    assert (address["enabled"], address["disabled_reason"]) == (False, "bounced: unregistered")

    again = await client.post("/v1/feedback", json=report)
    assert again.json()["disabled"] == 0
    later = await send(client, ["push"])
    await drain(runtime)
    unreachable = await message_of(client, later)
    assert (unreachable["status"], unreachable["status_reason"]) == (
        "suppressed",
        "no_reachable_channel",
    )
    assert len(push.sent) == 1


async def test_feedback_for_any_address_of_a_message_finds_it(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    devices = [{"channel": "push", "value": "token-1"}, {"channel": "push", "value": "token-2"}]
    await setup(client, addresses=devices)
    sent = await send(client, ["push"])
    await drain(runtime)
    assert [d.address for d in push.sent] == ["token-1", "token-2"]
    assert (await message_of(client, sent))["provider_message_id"] == "push-1"

    first = await client.post(
        "/v1/feedback",
        json={"type": "bounce", "channel": "push", "provider_message_id": "push-2"},
    )
    assert (first.json()["message_id"], first.json()["disabled"]) == (sent["id"], 1)
    addresses = await addresses_of(client)
    assert (addresses["token-1"]["enabled"], addresses["token-2"]["enabled"]) == (True, False)
    assert (await message_of(client, sent))["status"] == "sent"

    second = await client.post(
        "/v1/feedback",
        json={"type": "bounce", "channel": "push", "provider_message_id": "push-1"},
    )
    assert second.json()["disabled"] == 1
    assert (await message_of(client, sent))["status"] == "failed"


async def test_the_first_delivered_report_marks_the_message(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    devices = [{"channel": "push", "value": "token-1"}, {"channel": "push", "value": "token-2"}]
    await setup(client, addresses=devices)
    sent = await send(client, ["push"])
    await drain(runtime)
    delivered = await client.post(
        "/v1/feedback",
        json={"type": "delivered", "channel": "push", "provider_message_id": "push-2"},
    )
    assert delivered.json()["message_id"] == sent["id"]
    message = await message_of(client, sent)
    assert message["status"] == "delivered"
    bounce = {"type": "bounce", "channel": "push", "provider_message_id": "push-1"}
    await client.post("/v1/feedback", json=bounce)
    assert (await message_of(client, sent))["status"] == "delivered"


async def test_soft_bounces_in_a_row_disable_the_address(client: httpx.AsyncClient) -> None:
    await setup(client)
    soft = {"type": "soft_bounce", "channel": "sms", "address": "+15550001"}
    await client.post("/v1/feedback", json=soft)
    await client.post("/v1/feedback", json={**soft, "type": "delivered"})
    await client.post("/v1/feedback", json=soft)
    assert (await addresses_of(client))["+15550001"]["enabled"] is True
    result = await client.post("/v1/feedback", json={**soft, "reason": "mailbox full"})
    assert result.json()["disabled"] == 1
    address = (await addresses_of(client))["+15550001"]
    assert address["disabled_reason"] == "2 soft bounces in a row: mailbox full"


async def test_a_message_soft_bounces_an_address_once(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await setup(client)
    sent = await send(client, ["push"])
    await drain(runtime)
    soft = {"type": "soft_bounce", "channel": "push", "provider_message_id": "push-1"}
    for _ in range(3):
        assert (await client.post("/v1/feedback", json=soft)).json()["disabled"] == 0
    assert (await addresses_of(client))["token-1"]["enabled"] is True

    other = await send(client, ["push"], data={"order_id": "A-2"})
    await drain(runtime)
    assert (await message_of(client, other))["provider_message_id"] == "push-2"
    result = await client.post("/v1/feedback", json={**soft, "provider_message_id": "push-2"})
    assert result.json()["disabled"] == 1
    assert sent["id"] != other["id"]


async def test_a_repeated_report_id_is_ignored(client: httpx.AsyncClient) -> None:
    await setup(client)
    soft = {"type": "soft_bounce", "channel": "sms", "address": "+15550001", "report_id": "r-1"}
    assert (await client.post("/v1/feedback", json=soft)).json()["duplicate"] is False
    repeated = (await client.post("/v1/feedback", json=soft)).json()
    assert (repeated["duplicate"], repeated["recipient_count"]) == (True, 0)
    result = await client.post("/v1/feedback", json={**soft, "report_id": "r-2"})
    assert result.json()["disabled"] == 1


async def test_a_complaint_stops_optional_messages_to_the_address(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await setup(client)
    report = {"type": "complaint", "channel": "push", "address": "token-1"}
    result = await client.post("/v1/feedback", json=report)
    assert result.json()["unsubscribed"] is True
    assert (await client.post("/v1/feedback", json=report)).json()["unsubscribed"] is False
    addresses = await addresses_of(client)
    assert addresses["token-1"]["enabled"] is True
    assert addresses["token-1"]["complained_at"] is not None
    [change] = (await client.get("/v1/recipients/user-1/preferences/history")).json()["items"]
    assert change["source"] == "complaint"
    assert change["changes"] == {"complained": {"push": ["token-1"]}}
    assert change["provenance"]["method"] == "complaint"
    assert change["provenance"]["address"] == "token-1"

    created = await client.post("/v1/categories", json={"key": "news"})
    assert created.status_code in (200, 201), created.text
    receipt = await send(client, ["push"])
    news = await send(client, ["push"], category="news", data={"order_id": "A-2"})
    fallback = await send(client, ["push", "sms"], category="news", data={"order_id": "A-3"})
    await drain(runtime)
    assert (await message_of(client, receipt))["status"] == "sent"
    blocked = await message_of(client, news)
    assert (blocked["status"], blocked["status_reason"]) == ("suppressed", "no_reachable_channel")
    assert (await message_of(client, fallback))["channel"] == "sms"
    assert [d.address for d in push.sent] == ["token-1"]


async def test_delivered_feedback_marks_the_message(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await setup(client)
    sent = await send(client, ["push"])
    await drain(runtime)
    result = await client.post(
        "/v1/feedback", json={"type": "delivered", "channel": "push", "message_id": sent["id"]}
    )
    assert result.status_code == 200
    assert (await message_of(client, sent))["status"] == "delivered"
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
    async with keyed_client(runtime, key.json()["key"]) as reader:
        report = {"type": "bounce", "channel": "sms", "address": "+15550001"}
        assert (await reader.post("/v1/feedback", json=report)).status_code == 403


async def test_recipient_ids_need_recipients_read(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await setup(client)
    key = await client.post("/v1/api-keys", json={"name": "esp", "scopes": ["recipients:write"]})
    async with keyed_client(runtime, key.json()["key"]) as writer:
        report = {"type": "bounce", "channel": "sms", "address": "+15550001"}
        result = (await writer.post("/v1/feedback", json=report)).json()
    assert (result["recipients"], result["recipient_count"], result["disabled"]) == (None, 1, 1)


async def test_re_adding_an_address_clears_its_soft_bounces(client: httpx.AsyncClient) -> None:
    await setup(client)
    soft = {"type": "soft_bounce", "channel": "sms", "address": "+15550001"}
    await client.post("/v1/feedback", json=soft)
    revived = await client.post(
        "/v1/recipients/user-1/addresses", json={"channel": "sms", "value": "+15550001"}
    )
    assert revived.status_code in (200, 201), revived.text
    assert (await client.post("/v1/feedback", json=soft)).json()["disabled"] == 0


async def test_failing_channels_are_routed_around(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, sms: FakeChannel
) -> None:
    await setup(client)
    push.fail("token-1", retryable=True)
    first = await send(client, ["push", "sms"])
    await drain(runtime)
    assert (await message_of(client, first))["status"] == "queued"
    second = await send(client, ["push", "sms"])
    third = await send(client, ["push", "sms"])
    await drain(runtime)
    for sent in (second, third):
        message = await message_of(client, sent)
        assert (message["status"], message["channel"]) == ("sent", "sms")
    assert not push.sent
    assert runtime.health.states["push"].failures == 2

    only = await send(client, ["push"])
    await drain(runtime)
    assert (await message_of(client, only))["status"] == "queued"


async def test_deferred_recipients_do_not_open_the_breaker(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await setup(client)
    push.failures["token-1"] = DeliveryError("450 greylisted", retryable=True, deferred=True)
    for index in range(3):
        await send(client, ["push", "sms"], data={"order_id": f"A-{index}"})
    await drain(runtime)
    assert "push" not in runtime.health.states


async def test_the_last_attempt_still_tries_an_unhealthy_first_choice(
    settings: Settings,
    client: httpx.AsyncClient,
    admin_key: str,
    push: FakeChannel,
    sms: FakeChannel,
) -> None:
    await setup(client)
    worker = settings.worker.model_copy(update={"max_attempts": 1})
    last = Runtime.create(
        settings.model_copy(update={"worker": worker}), channels={"push": push, "sms": sms}
    )
    try:
        for _ in range(2):
            last.health.failed("push")
        assert not last.health.available("push")
        async with keyed_client(last, admin_key) as sender:
            sent = await send(sender, ["push", "sms"])
        await drain(last)
        message = await message_of(client, sent)
        assert (message["status"], message["channel"]) == ("sent", "push")
        assert "push" not in last.health.states
    finally:
        await last.aclose()


def signature(token: str, url: str, params: dict[str, str]) -> str:
    data = url + "".join(name + params[name] for name in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


@pytest.fixture
async def twilio(settings: Settings, push: FakeChannel) -> AsyncIterator[Runtime]:
    sids = iter(["SM1", "SM2", "SM3"])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"sid": next(sids)})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = TwilioConfig(account_sid="AC1", auth_token="twilio-token", from_number="+15550000000")
    alerts = TwilioConfig(account_sid="AC1", auth_token="twilio-token", from_number="+15559999999")
    rt = Runtime.create(
        settings,
        channels={
            "push": push,
            "sms": TwilioChannel("sms", config, http),
            "alerts": TwilioChannel("alerts", alerts, http),
        },
        http=http,
    )
    try:
        yield rt
    finally:
        await rt.aclose()


async def test_twilio_status_callbacks_are_feedback(
    client: httpx.AsyncClient, twilio: Runtime, admin_key: str
) -> None:
    await setup(client)
    async with keyed_client(twilio, admin_key) as sender:
        await create_recipient(
            sender,
            "user-2",
            addresses=[{"channel": "sms", "value": PHONE}, {"channel": "alerts", "value": PHONE}],
        )
        first = await send(sender, ["sms"])
        second = await send(sender, ["alerts"], recipient="user-2")
        third = await send(sender, ["sms"], recipient="user-2")
    await drain(twilio)
    sid = {
        name: (await message_of(client, sent))["provider_message_id"]
        for name, sent in (("first", first), ("second", second), ("third", third))
    }

    app = create_app(runtime=twilio)
    app.state.runtime = twilio
    path = "/v1/inbound/twilio/status"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cue.test"
    ) as server:

        async def report(query: str = "", **params: str) -> httpx.Response:
            form = {"AccountSid": "AC1", "ErrorCode": "", **params}
            url = f"{BASE}{path}{query}"
            return await server.post(
                f"{path}{query}",
                data=form,
                headers={"X-Twilio-Signature": signature("twilio-token", url, form)},
            )

        delivered = await report(MessageSid=sid["first"], MessageStatus="delivered", To="+15550001")
        assert delivered.status_code == 204
        dead = await report(
            "?tenant=a&b=1",
            MessageSid=sid["second"],
            MessageStatus="undelivered",
            ErrorCode="30005",
            To=PHONE,
        )
        assert dead.status_code == 204
        for _ in range(2):
            soft = await report(
                MessageSid=sid["third"],
                MessageStatus="undelivered",
                ErrorCode="30003",
                To=PHONE,
            )
            assert soft.status_code == 204
        stranger = await report(
            MessageSid="SM9", MessageStatus="undelivered", ErrorCode="30005", To="+15550001"
        )
        assert stranger.status_code == 204
        unsigned_query = await server.post(
            f"{path}?tenant=a",
            data={"AccountSid": "AC1", "MessageSid": "SM1", "MessageStatus": "failed"},
            headers={
                "X-Twilio-Signature": signature(
                    "twilio-token",
                    f"{BASE}{path}",
                    {"AccountSid": "AC1", "MessageSid": "SM1", "MessageStatus": "failed"},
                )
            },
        )
        assert unsigned_query.status_code == 403
        forged = await server.post(
            path,
            data={"AccountSid": "AC1", "MessageSid": "SM1", "MessageStatus": "failed"},
            headers={"X-Twilio-Signature": "nope"},
        )
        assert forged.status_code == 403
        unknown = await server.post(
            path,
            data={"AccountSid": "ACé", "MessageSid": "SM1"},
            headers={"X-Twilio-Signature": "nope"},
        )
        assert unknown.status_code == 404

    assert (await message_of(client, first))["status"] == "delivered"
    failed = await message_of(client, second)
    assert (failed["status"], failed["channel"], failed["status_reason"]) == (
        "failed",
        "alerts",
        "bounced: twilio undelivered 30005",
    )
    recipient = (await client.get("/v1/recipients/user-2")).json()
    states = {(a["channel"], a["enabled"]) for a in recipient["addresses"]}
    assert states == {("sms", True), ("alerts", False)}
    assert (await message_of(client, third))["status"] == "sent"
    assert (await addresses_of(client))["+15550001"]["enabled"] is True
