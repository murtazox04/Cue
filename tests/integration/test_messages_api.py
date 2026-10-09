from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from cue_notify.runtime import Runtime
from tests.helpers import FakeChannel, create_recipient, create_template, drain


def _send(**overrides: object) -> dict[str, object]:
    return {
        "recipient": "user-1",
        "template": "order-shipped",
        "channels": ["push"],
        "data": {"order_id": "Z-9"},
        **overrides,
    }


async def test_direct_send(client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel) -> None:
    await create_template(client)
    await create_recipient(client)
    response = await client.post("/v1/messages", json=_send())
    assert response.status_code == 202, response.text
    message = response.json()
    assert message["status"] == "queued"
    assert message["category"] == "transactional"
    assert message["content"]["default"]["body"] == "Order Z-9 is on its way."

    await drain(runtime)
    sent = (await client.get(f"/v1/messages/{message['id']}")).json()
    assert sent["status"] == "sent"
    assert sent["provider_message_id"] == "push-1"
    assert sent["sent_at"] is not None


async def test_direct_send_idempotency(client: httpx.AsyncClient, runtime: Runtime) -> None:
    await create_template(client)
    await create_recipient(client)
    first = await client.post("/v1/messages", json=_send(idempotency_key="otp-1"))
    second = await client.post("/v1/messages", json=_send(idempotency_key="otp-1"))
    assert (first.status_code, second.status_code) == (202, 200)
    assert first.json()["id"] == second.json()["id"]


async def test_unknown_channel_is_rejected(client: httpx.AsyncClient) -> None:
    await create_template(client)
    response = await client.post("/v1/messages", json=_send(channels=["pigeon"]))
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert "pigeon" in response.json()["detail"]


async def test_scheduled_send_can_be_cancelled(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_template(client)
    await create_recipient(client)
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    message = (await client.post("/v1/messages", json=_send(send_at=later))).json()
    assert await drain(runtime) == 0  # not due yet

    cancelled = await client.post(f"/v1/messages/{message['id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    again = await client.post(f"/v1/messages/{message['id']}/cancel")
    assert again.status_code == 409


async def test_engagement_and_tracking_token(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_template(client)
    await create_recipient(client)
    message = (await client.post("/v1/messages", json=_send())).json()

    early = await client.post(f"/v1/messages/{message['id']}/engagement", json={"type": "opened"})
    assert early.status_code == 409  # not sent yet

    await drain(runtime)
    token = push.sent[0].metadata["cue_tracking_token"]

    # Public tracking endpoint: no API key, just the per-message token.
    anonymous = httpx.AsyncClient(transport=client._transport, base_url=str(client.base_url))
    async with anonymous:
        clicked = await anonymous.post(f"/v1/track/{token}", json={"type": "clicked"})
        assert clicked.status_code == 204
        unknown = await anonymous.post("/v1/track/not-a-token", json={"type": "opened"})
        assert unknown.status_code == 404

    tracked = (await client.get(f"/v1/messages/{message['id']}")).json()
    assert tracked["status"] == "delivered"
    assert tracked["delivered_at"] and tracked["opened_at"] and tracked["clicked_at"]
    assert tracked["converted_at"] is None


async def test_list_messages_paginates(client: httpx.AsyncClient, runtime: Runtime) -> None:
    await create_template(client)
    await create_recipient(client)
    for index in range(5):
        await client.post("/v1/messages", json=_send(idempotency_key=f"k{index}"))
    first = (await client.get("/v1/messages", params={"limit": 2})).json()
    assert len(first["items"]) == 2
    second = (
        await client.get("/v1/messages", params={"limit": 2, "cursor": first["next_cursor"]})
    ).json()
    third = (
        await client.get("/v1/messages", params={"limit": 2, "cursor": second["next_cursor"]})
    ).json()
    ids = [m["id"] for page in (first, second, third) for m in page["items"]]
    assert len(ids) == len(set(ids)) == 5
    assert third["next_cursor"] is None


async def test_template_preview_and_validation(client: httpx.AsyncClient) -> None:
    await create_template(client)
    preview = await client.post(
        "/v1/templates/order-shipped/preview", json={"locale": "en", "data": {"order_id": "P-1"}}
    )
    assert preview.status_code == 200
    assert preview.json()["variants"]["sms"]["body"] == "Order P-1 shipped."

    broken = await client.post(
        "/v1/templates",
        json={"key": "broken", "locales": {"en": {"body": "Hi {{ name"}}},
    )
    assert broken.status_code == 422

    missing_default = await client.post(
        "/v1/templates",
        json={"key": "x", "default_locale": "ru", "locales": {"en": {"body": "Hi"}}},
    )
    assert missing_default.status_code == 422


async def test_template_versions_and_conflicts(client: httpx.AsyncClient) -> None:
    created = await create_template(client)
    assert created["version"] == 1
    body = {k: v for k, v in created.items() if k in ("default_locale", "locales")}
    unchanged = await client.put("/v1/templates/order-shipped", json=body)
    assert unchanged.json()["version"] == 1  # identical content is not a new version
    body["locales"]["en"]["title"] = "Shipped!"
    updated = await client.put("/v1/templates/order-shipped", json=body)
    assert updated.json()["version"] == 2
    duplicate = await client.post("/v1/templates", json={**body, "key": "order-shipped"})
    assert duplicate.status_code == 409


async def test_invalid_rule_condition_is_rejected(client: httpx.AsyncClient) -> None:
    await create_template(client)
    response = await client.post(
        "/v1/rules",
        json={
            "key": "bad",
            "name": "Bad",
            "event": "x",
            "template": "order-shipped",
            "category": "transactional",
            "channels": ["push"],
            "condition": {"eval": ["rm -rf /"]},
        },
    )
    assert response.status_code == 422
    assert "unknown operator" in response.text


async def test_recipient_erasure(client: httpx.AsyncClient, runtime: Runtime) -> None:
    await create_template(client)
    await create_recipient(client)
    await client.post("/v1/messages", json=_send())
    assert (await client.delete("/v1/recipients/user-1")).status_code == 204
    assert (await client.get("/v1/recipients/user-1")).status_code == 404
    assert (await client.get("/v1/messages")).json()["items"] == []
