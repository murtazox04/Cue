from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select, update

from cue_notify.db.models import Job
from cue_notify.runtime import Runtime
from cue_notify.settings import (
    ComplianceSettings,
    DatabaseSettings,
    PolicySettings,
    Settings,
    WorkerSettings,
)
from tests.helpers import (
    FakeChannel,
    create_recipient,
    create_rule,
    create_template,
    drain,
    send_event,
)


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        environment="test",
        database=DatabaseSettings(url=database_url, pool_size=5),
        worker=WorkerSettings(concurrency=4, retry_base_seconds=1, retry_max_seconds=2),
        policy=PolicySettings(default_locale="en", default_timezone="UTC", quiet_hours=None),
        compliance=ComplianceSettings(us_texting_rules=True),
    )


async def send(client: httpx.AsyncClient, **fields: Any) -> dict[str, Any]:
    body = {
        "recipient": "user-1",
        "template": "order-shipped",
        "channels": ["push"],
        "category": "marketing",
        "data": {"order_id": "A-1"},
        **fields,
    }
    response = await client.post("/v1/messages", json=body)
    assert response.status_code == 202, response.text
    return response.json()


async def message(client: httpx.AsyncClient, message_id: str) -> dict[str, Any]:
    response = await client.get(f"/v1/messages/{message_id}")
    assert response.status_code == 200, response.text
    return response.json()


def quiet_around_now() -> dict[str, str]:
    now = datetime.now(UTC)
    start = (now - timedelta(hours=1)).time().replace(second=0, microsecond=0)
    end = (now + timedelta(hours=2)).time().replace(second=0, microsecond=0)
    return {"start": start.isoformat(), "end": end.isoformat()}


async def deliver_job(runtime: Runtime) -> Job:
    async with runtime.session() as session:
        job = await session.scalar(select(Job).where(Job.kind == "deliver"))
    assert job is not None
    return job


async def make_due(runtime: Runtime) -> None:
    async with runtime.transaction() as session:
        await session.execute(update(Job).values(run_at=datetime.now(UTC)))


@pytest.fixture
async def person(client: httpx.AsyncClient) -> None:
    await create_template(client)
    await create_recipient(client, timezone="UTC")


@pytest.mark.usefixtures("person")
async def test_quiet_hours_are_checked_again_right_before_sending(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    queued = await send(client)
    assert queued["status"] == "queued"
    preferences = {"quiet_hours": quiet_around_now()}
    assert (
        await client.put("/v1/recipients/user-1/preferences", json=preferences)
    ).status_code == 200

    await drain(runtime)
    assert push.sent == []
    waiting = await message(client, queued["id"])
    assert waiting["status"] == "queued"
    send_at = datetime.fromisoformat(waiting["send_at"])
    assert send_at > datetime.now(UTC) + timedelta(minutes=90)
    job = await deliver_job(runtime)
    assert abs(job.run_at - send_at) < timedelta(minutes=1)

    awake = {"quiet_hours": {"start": "00:00", "end": "00:00"}}
    await client.put("/v1/recipients/user-1/preferences", json=awake)
    await make_due(runtime)
    await drain(runtime)
    assert len(push.sent) == 1
    assert (await message(client, queued["id"]))["status"] == "sent"


@pytest.mark.usefixtures("person")
async def test_critical_messages_still_break_through_at_send_time(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    security = {"key": "security", "allow_unsubscribe": False, "respect_quiet_hours": True}
    assert (await client.post("/v1/categories", json=security)).status_code == 201
    queued = await send(client, category="security", importance="critical")
    await client.put("/v1/recipients/user-1/preferences", json={"quiet_hours": quiet_around_now()})
    await drain(runtime)
    assert len(push.sent) == 1
    assert (await message(client, queued["id"]))["status"] == "sent"


@pytest.mark.usefixtures("person")
async def test_a_message_held_past_its_expiry_at_send_time_is_dropped(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    expires_at = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    queued = await send(client, expires_at=expires_at)
    await client.put("/v1/recipients/user-1/preferences", json={"quiet_hours": quiet_around_now()})
    await drain(runtime)
    assert push.sent == []
    dropped = await message(client, queued["id"])
    assert (dropped["status"], dropped["status_reason"]) == ("suppressed", "expired")


def zone_at_three_in_the_morning() -> str:
    hour = datetime.now(UTC).hour
    offset = next(o for o in range(-12, 15) if (hour + o) % 24 == 3)
    if offset == 0:
        return "Etc/GMT"
    return f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"


async def test_legal_texting_hours_are_checked_again_right_before_sending(
    client: httpx.AsyncClient, runtime: Runtime, sms: FakeChannel
) -> None:
    zone = zone_at_three_in_the_morning()
    await create_template(client)
    await create_recipient(
        client,
        external_id="shopper",
        timezone=zone,
        region="DE",
        addresses=[{"channel": "sms", "value": "+15551230000"}],
    )
    queued = await send(client, recipient="shopper", channels=["sms"])
    assert queued["status"] == "queued"
    moved = await client.put("/v1/recipients/shopper", json={"region": "US-FL"})
    assert moved.status_code == 200, moved.text

    await drain(runtime)
    assert sms.sent == []
    waiting = await message(client, queued["id"])
    assert waiting["status"] == "queued"
    local = datetime.fromisoformat(waiting["send_at"]).astimezone(ZoneInfo(zone))
    assert local.time() == time(8, 0)
    assert local > datetime.now(UTC)


@pytest.mark.usefixtures("person")
async def test_messages_scheduled_for_later_only_count_near_their_send_time(
    client: httpx.AsyncClient,
) -> None:
    news = {"key": "news", "frequency_caps": [{"period": "P1D", "limit": 1}]}
    assert (await client.post("/v1/categories", json=news)).status_code == 201
    later = datetime.now(UTC) + timedelta(days=10)
    scheduled = await send(client, category="news", send_at=later.isoformat())
    today = await send(client, category="news")
    crowded = await send(client, category="news", send_at=(later + timedelta(hours=1)).isoformat())
    assert [scheduled["status"], today["status"], crowded["status"]] == [
        "queued",
        "queued",
        "suppressed",
    ]
    assert crowded["status_reason"] == "frequency_cap: 1 per 1 day, 0:00:00"


@pytest.mark.usefixtures("person")
async def test_a_delayed_message_does_not_put_todays_in_cooldown(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_rule(client, cooldown_seconds=3600, delay_seconds=10 * 86_400)
    await send_event(client)
    await drain(runtime)
    patched = await client.patch("/v1/rules/notify-shipped", json={"delay_seconds": 0})
    assert patched.status_code == 200, patched.text
    await send_event(client)
    await drain(runtime)
    statuses = sorted(str(m["status"]) for m in (await client.get("/v1/messages")).json()["items"])
    assert statuses == ["queued", "sent"]
    assert len(push.sent) == 1
