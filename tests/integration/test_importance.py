"""Importance, expiry and actions: structured messages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import update

from cue_notify.core.enums import Importance
from cue_notify.core.policy import QuietHours
from cue_notify.db.models import Message
from cue_notify.runtime import Runtime
from tests.helpers import FakeChannel, create_recipient, create_rule, create_template, drain


def _quiet_now(runtime: Runtime) -> None:
    now = datetime.now(UTC)
    runtime.settings.policy.quiet_hours = QuietHours(
        start=(now - timedelta(hours=1)).time().replace(second=0, microsecond=0),
        end=(now + timedelta(hours=2)).time().replace(second=0, microsecond=0),
    )


async def _category(client: httpx.AsyncClient, key: str, *, locked: bool) -> None:
    response = await client.post(
        "/v1/categories",
        json={
            "key": key,
            "respect_quiet_hours": True,
            "allow_unsubscribe": not locked,
            "frequency_caps": [{"period": "P1D", "limit": 1}],
        },
    )
    assert response.status_code == 201, response.text


async def _send(client: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    body = {
        "recipient": "user-1",
        "template": "order-shipped",
        "channels": ["push"],
        "data": {"order_id": "A-1"},
        **fields,
    }
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 202, response.text
    return response.json()


async def test_critical_breaks_through_quiet_hours_and_caps_where_people_cannot_opt_out(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    _quiet_now(runtime)
    await _category(client, "security", locked=True)
    await create_template(client)
    await create_recipient(client, timezone="UTC")

    normal = await _send(client, category="security")
    assert normal["status"] == "queued"
    assert datetime.fromisoformat(normal["send_at"]) > datetime.now(UTC) + timedelta(minutes=50)

    critical = await _send(client, category="security", importance="critical")
    assert critical["status"] == "queued"
    assert critical["importance"] == "critical"
    await drain(runtime)
    [delivered] = push.sent
    assert delivered.importance is Importance.CRITICAL

    capped = await _send(client, category="security", importance="high")
    assert capped["status"] == "suppressed"
    assert capped["status_reason"].startswith("frequency_cap")


async def test_critical_is_downgraded_where_people_can_unsubscribe(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    _quiet_now(runtime)
    await _category(client, "promotions", locked=False)
    await create_template(client)
    await create_recipient(client, timezone="UTC")
    message = await _send(client, category="promotions", importance="critical")
    assert message["importance"] == "high"
    assert datetime.fromisoformat(message["send_at"]) > datetime.now(UTC) + timedelta(minutes=50)


async def test_messages_that_would_arrive_too_late_are_dropped(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    _quiet_now(runtime)
    await _category(client, "rides", locked=False)
    await create_template(client)
    await create_recipient(client, timezone="UTC")
    soon = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    message = await _send(client, category="rides", expires_at=soon)
    assert message["status"] == "suppressed"
    assert message["status_reason"] == "expired"


async def test_expiry_is_checked_again_right_before_sending(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_template(client)
    await create_recipient(client)
    later = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    message = await _send(client, expires_at=later)
    assert message["status"] == "queued"
    async with runtime.transaction() as session:  # the worker was slow
        await session.execute(
            update(Message).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    await drain(runtime)
    assert push.sent == []
    stored = (await client.get(f"/v1/messages/{message['id']}")).json()
    assert (stored["status"], stored["status_reason"]) == ("suppressed", "expired")


async def test_rules_set_importance_expiry_and_actions(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_template(
        client,
        locales={
            "en": {
                "title": "Order {{ data.order_id }} shipped",
                "body": "{{ importance }}",
                "actions": [
                    {"id": "track", "label": "Track", "url": "https://t.test/{{ data.order_id }}"},
                    {"id": "dismiss", "label": "Dismiss"},
                ],
            }
        },
    )
    await create_recipient(client)
    rule = await create_rule(
        client, channels=["push"], importance="high", expires_after_seconds=600
    )
    assert (rule["importance"], rule["expires_after_seconds"]) == ("high", 600)
    await client.post(
        "/v1/events",
        json={"name": "order.shipped", "recipient": "user-1", "data": {"order_id": "A-7"}},
    )
    await drain(runtime)

    [delivered] = push.sent
    assert delivered.importance is Importance.HIGH
    assert delivered.content.body == "high"
    assert [(a.id, a.label, a.url) for a in delivered.content.actions] == [
        ("track", "Track", "https://t.test/A-7"),
        ("dismiss", "Dismiss", None),
    ]
    assert delivered.expires_at is not None
    remaining = delivered.expires_at - datetime.now(UTC)
    assert timedelta(minutes=9) < remaining <= timedelta(minutes=10)


async def test_invalid_actions_are_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/templates",
        json={
            "key": "bad",
            "locales": {"en": {"body": "x", "actions": [{"id": "has space", "label": "A"}]}},
        },
    )
    assert response.status_code == 422
    too_many = [{"id": f"a{n}", "label": "A"} for n in range(5)]
    response = await client.post(
        "/v1/templates", json={"key": "bad", "locales": {"en": {"body": "x", "actions": too_many}}}
    )
    assert response.status_code == 422
