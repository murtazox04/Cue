"""End-to-end: event in → rule → policy → delivery out."""

from __future__ import annotations

import httpx

from cue_notify.runtime import Runtime
from tests.helpers import (
    FakeChannel,
    create_recipient,
    create_rule,
    create_template,
    drain,
    send_event,
)


async def _setup(client: httpx.AsyncClient, **rule: object) -> None:
    await create_template(client)
    await create_rule(client, **rule)
    await create_recipient(client)


async def test_event_is_delivered_on_first_channel(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, sms: FakeChannel
) -> None:
    await _setup(client)

    response = await send_event(client, idempotency_key="evt-1")
    assert response.status_code == 202
    event_id = response.json()["id"]

    assert await drain(runtime) == 2  # process_event + deliver

    assert [d.address for d in push.sent] == ["token-1"]
    assert sms.sent == []
    delivered = push.sent[0]
    assert delivered.content.title == "Order shipped"
    assert delivered.content.body == "Order A-1 is on its way."
    assert delivered.content.url == "https://shop.test/orders/A-1"
    assert delivered.metadata["cue_message_id"]

    event = (await client.get(f"/v1/events/{event_id}")).json()
    assert event["status"] == "processed"
    assert event["outcome"][0]["rule"] == "notify-shipped"
    assert event["outcome"][0]["status"] == "queued"

    messages = (await client.get("/v1/messages", params={"event_id": event_id})).json()["items"]
    assert len(messages) == 1
    assert messages[0]["status"] == "sent"
    assert messages[0]["channel"] == "push"


async def test_idempotency_key_prevents_duplicates(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await _setup(client)
    first = await send_event(client, idempotency_key="same")
    second = await send_event(client, idempotency_key="same")
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {**first.json(), "duplicate": True}
    await drain(runtime)
    assert len(push.sent) == 1


async def test_inline_recipient_profile_is_upserted(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_template(client)
    await create_rule(client)
    recipient = {
        "external_id": "new-user",
        "locale": "uz-Latn-UZ",
        "addresses": [{"channel": "push", "value": "tok-new"}],
    }
    assert (await send_event(client, recipient=recipient)).status_code == 202
    await drain(runtime)
    assert push.sent[0].address == "tok-new"
    assert push.sent[0].content.body == "A-1 buyurtmangiz yo'lga chiqdi."  # uz-Latn-UZ → uz
    assert push.sent[0].locale == "uz"


async def test_condition_and_priority(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, sms: FakeChannel
) -> None:
    await create_template(client)
    await create_recipient(client)
    await create_rule(
        client,
        key="big-orders-by-sms",
        priority=10,
        channels=["sms"],
        condition={">": [{"var": "event.data.total"}, 1000]},
    )
    await create_rule(client, key="default-push", priority=0, channels=["push"])

    await send_event(client, data={"order_id": "A-1", "total": 5000})
    await send_event(client, data={"order_id": "A-2", "total": 10})
    await drain(runtime)

    assert [d.content.body for d in sms.sent] == ["Order A-1 shipped."]  # SMS override
    assert [d.content.body for d in push.sent] == ["Order A-2 is on its way."]


async def test_rules_without_stop_processing_all_fire(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, sms: FakeChannel
) -> None:
    await create_template(client)
    await create_recipient(client)
    await create_rule(client, key="a", channels=["sms"], stop_processing=False, priority=1)
    await create_rule(client, key="b", channels=["push"])
    await send_event(client)
    await drain(runtime)
    assert len(sms.sent) == 1
    assert len(push.sent) == 1


async def test_wildcard_event_and_zero_rollout(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_template(client)
    await create_recipient(client)
    await create_rule(client, key="dark", event="order.*", rollout_percent=0, priority=5)
    await create_rule(client, key="live", event="order.*")
    response = await send_event(client)
    await drain(runtime)
    outcome = (await client.get(f"/v1/events/{response.json()['id']}")).json()["outcome"]
    assert outcome[0] == {"rule": "dark", "matched": False, "reason": "outside_rollout"}
    assert outcome[1]["rule"] == "live"
    assert len(push.sent) == 1


async def test_dry_run_evaluation_sends_nothing(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await _setup(client, condition={"==": [{"var": "event.data.kind"}, "express"]})
    response = await client.post(
        "/v1/rules/evaluate",
        json={
            "name": "order.shipped",
            "recipient": "user-1",
            "data": {"kind": "express", "order_id": "X"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == [
        {"rule": "notify-shipped", "matched": True, "status": "queued", "reason": None}
    ]
    assert await drain(runtime) == 0
    assert push.sent == []


async def test_missing_template_variable_fails_message_visibly(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await _setup(client)
    await send_event(client, data={})  # template needs data.order_id
    await drain(runtime)
    messages = (await client.get("/v1/messages")).json()["items"]
    assert messages[0]["status"] == "failed"
    assert messages[0]["status_reason"].startswith("template_error")
    assert push.sent == []
