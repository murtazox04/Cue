"""Delivery policy: preferences, reachability, caps, cooldowns, quiet hours, fallback."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from cue_notify.core.policy import QuietHours
from cue_notify.db.models import Job
from cue_notify.runtime import Runtime
from tests.helpers import (
    FakeChannel,
    create_recipient,
    create_rule,
    create_template,
    drain,
    send_event,
)


async def _messages(client: httpx.AsyncClient) -> list[dict[str, object]]:
    return (await client.get("/v1/messages")).json()["items"]


@pytest.fixture
async def setup(client: httpx.AsyncClient) -> None:
    await create_template(client)
    await create_recipient(client)


@pytest.mark.usefixtures("setup")
async def test_unsubscribed_category_is_suppressed(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_rule(client, category="marketing")
    response = await client.put(
        "/v1/recipients/user-1/preferences", json={"unsubscribed_categories": ["marketing"]}
    )
    assert response.status_code == 200
    await send_event(client)
    await drain(runtime)
    [message] = await _messages(client)
    assert message["status"] == "suppressed"
    assert message["status_reason"] == "unsubscribed"
    assert push.sent == []


@pytest.mark.usefixtures("setup")
async def test_transactional_ignores_unsubscribe(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_rule(client, category="transactional")
    await client.put(
        "/v1/recipients/user-1/preferences", json={"unsubscribed_categories": ["transactional"]}
    )
    await send_event(client)
    await drain(runtime)
    assert len(push.sent) == 1


@pytest.mark.usefixtures("setup")
async def test_muted_channel_falls_back_to_next(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, sms: FakeChannel
) -> None:
    await create_rule(client)
    await client.put("/v1/recipients/user-1/preferences", json={"muted_channels": ["push"]})
    await send_event(client)
    await drain(runtime)
    assert push.sent == []
    assert [d.address for d in sms.sent] == ["+15550001"]


async def test_no_reachable_channel(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_template(client)
    await create_rule(client, channels=["push"])
    await create_recipient(client, addresses=[{"channel": "sms", "value": "+1555"}])
    await send_event(client)
    await drain(runtime)
    [message] = await _messages(client)
    assert message["status_reason"] == "no_reachable_channel"


@pytest.mark.usefixtures("setup")
async def test_frequency_cap(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    response = await client.post(
        "/v1/categories",
        json={"key": "digest", "frequency_caps": [{"period": "P1D", "limit": 2}]},
    )
    assert response.status_code == 201, response.text
    await create_rule(client, category="digest", stop_processing=True)
    for _ in range(3):
        await send_event(client)
    await drain(runtime)
    statuses = sorted(m["status"] for m in await _messages(client))
    assert statuses == ["sent", "sent", "suppressed"]
    assert len(push.sent) == 2


@pytest.mark.usefixtures("setup")
async def test_rule_cooldown(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_rule(client, cooldown_seconds=3600)
    await send_event(client)
    await send_event(client)
    await drain(runtime)
    reasons = sorted(str(m["status_reason"]) for m in await _messages(client))
    assert reasons == ["None", "cooldown"]
    assert len(push.sent) == 1


async def test_quiet_hours_defer_marketing(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    now = datetime.now(UTC)
    # A quiet window that certainly contains "now" in the recipient's zone.
    runtime.settings.policy.quiet_hours = QuietHours(
        start=(now - timedelta(hours=1)).time().replace(second=0, microsecond=0),
        end=(now + timedelta(hours=2)).time().replace(second=0, microsecond=0),
    )
    await create_template(client)
    await create_recipient(client, timezone="UTC")
    await create_rule(client, category="marketing")
    await send_event(client)
    await drain(runtime)

    [message] = await _messages(client)
    assert message["status"] == "queued"
    send_at = datetime.fromisoformat(str(message["send_at"]))
    assert send_at > now + timedelta(minutes=90)
    assert push.sent == []  # delivery job exists but is not due yet
    async with runtime.session() as session:
        jobs = (await session.scalars(select(Job))).all()
    assert [job.kind for job in jobs] == ["deliver"]


async def test_quiet_hours_do_not_delay_transactional(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    now = datetime.now(UTC)
    runtime.settings.policy.quiet_hours = QuietHours(
        start=(now - timedelta(hours=1)).time(), end=(now + timedelta(hours=2)).time()
    )
    await create_template(client)
    await create_recipient(client)
    await create_rule(client, category="transactional")
    await send_event(client)
    await drain(runtime)
    assert len(push.sent) == 1


@pytest.mark.usefixtures("setup")
async def test_permanent_failure_falls_back_to_next_channel(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, sms: FakeChannel
) -> None:
    await create_rule(client)
    push.fail("token-1")
    await send_event(client)
    await drain(runtime)
    [message] = await _messages(client)
    assert message["status"] == "sent"
    assert message["channel"] == "sms"


@pytest.mark.usefixtures("setup")
async def test_invalid_address_is_disabled(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, sms: FakeChannel
) -> None:
    await create_rule(client)
    push.fail("token-1", invalid=True)
    await send_event(client)
    await drain(runtime)
    recipient = (await client.get("/v1/recipients/user-1")).json()
    push_address = next(a for a in recipient["addresses"] if a["channel"] == "push")
    assert push_address["enabled"] is False
    assert push_address["disabled_reason"] == "gone"
    assert len(sms.sent) == 1


@pytest.mark.usefixtures("setup")
async def test_multi_device_fanout(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_rule(client)
    await client.post(
        "/v1/recipients/user-1/addresses", json={"channel": "push", "value": "token-2"}
    )
    await send_event(client)
    await drain(runtime)
    assert sorted(d.address for d in push.sent) == ["token-1", "token-2"]


@pytest.mark.usefixtures("setup")
async def test_transient_failure_is_retried_not_fallen_back(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, sms: FakeChannel
) -> None:
    await create_rule(client)
    push.fail("token-1", retryable=True)
    await send_event(client)
    await drain(runtime)
    [message] = await _messages(client)
    assert message["status"] == "queued"
    assert sms.sent == []
    async with runtime.session() as session:
        job = await session.scalar(select(Job).where(Job.kind == "deliver"))
    assert job is not None
    assert job.attempts == 1
    assert job.run_at > datetime.now(UTC)
    assert job.last_error is not None

    # The provider recovers; the retry succeeds on the same channel.
    push.failures.clear()
    async with runtime.transaction() as session:
        stored = await session.get(Job, job.id)
        assert stored is not None
        stored.run_at = datetime.now(UTC)
    await drain(runtime)
    [message] = await _messages(client)
    assert message["status"] == "sent"
    assert message["channel"] == "push"


@pytest.mark.usefixtures("setup")
async def test_last_attempt_falls_back_after_transient_failures(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, sms: FakeChannel
) -> None:
    runtime.settings.worker.max_attempts = 1
    await create_rule(client)
    push.fail("token-1", retryable=True)
    await send_event(client)
    await drain(runtime)
    [message] = await _messages(client)
    assert message["channel"] == "sms"
