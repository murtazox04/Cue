from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from typer.testing import CliRunner

from cue_notify import jobs, queue
from cue_notify.core.enums import BroadcastStatus, DigestStatus, EventStatus, MessageStatus
from cue_notify.db.models import Broadcast, Digest, Event, Job, Message, Recipient
from cue_notify.runtime import Runtime
from cue_notify.services import broadcasts, digests, events
from cue_notify.services.composer import DELIVER_JOB
from tests.conftest import POSTGRES_URL
from tests.helpers import (
    FakeChannel,
    create_recipient,
    create_rule,
    create_template,
    drain,
    run_cli,
    send_event,
)

postgres_only = pytest.mark.skipif(not POSTGRES_URL, reason="needs CUE_TEST_DATABASE_URL")

SEND = {
    "recipient": "user-1",
    "template": "order-shipped",
    "channels": ["push"],
    "data": {"order_id": "1"},
}
DIGEST_TEMPLATE = {"key": "comments", "locales": {"en": {"body": "{{ data.count }} comments"}}}


async def explode(_: queue.JobContext) -> None:
    raise RuntimeError("database on fire")


def failing_worker(runtime: Runtime, *kinds: str) -> queue.Worker:
    handlers = {**jobs.HANDLERS, **dict.fromkeys(kinds, explode)}
    return queue.Worker(runtime, handlers, on_dead=jobs.ON_DEAD)


async def dead_jobs(runtime: Runtime) -> list[Job]:
    async with runtime.session() as session:
        return list((await session.scalars(select(Job).where(Job.dead.is_(True)))).all())


async def revive(runtime: Runtime) -> queue.Revival:
    async with runtime.transaction() as session:
        return await jobs.revive(session)


async def load[T](runtime: Runtime, model: type[T], row_id: Any) -> T:
    async with runtime.session() as session:
        row = await session.get(model, UUID(str(row_id)))
    assert row is not None
    return row


async def test_retry_dead_reopens_events_and_messages(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel, cli_runner: CliRunner
) -> None:
    runtime.settings.worker.max_attempts = 1
    await create_template(client)
    await create_recipient(client)
    await create_rule(client)
    event = (await send_event(client)).json()
    message = (await client.post("/v1/messages", json=SEND)).json()
    await failing_worker(runtime, events.PROCESS_JOB, DELIVER_JOB).run_until_idle()
    assert (await load(runtime, Event, event["id"])).status is EventStatus.FAILED
    assert (await load(runtime, Message, message["id"])).status is MessageStatus.FAILED

    assert "requeued 2 job(s), reopened 2 row(s)" in await run_cli(cli_runner, "jobs", "retry-dead")
    reopened_event = await load(runtime, Event, event["id"])
    reopened_message = await load(runtime, Message, message["id"])
    assert (reopened_event.status, reopened_event.error) == (EventStatus.PENDING, None)
    assert (reopened_message.status, reopened_message.status_reason) == (MessageStatus.QUEUED, None)

    await drain(runtime)
    assert (await load(runtime, Event, event["id"])).status is EventStatus.PROCESSED
    assert (await load(runtime, Message, message["id"])).status is MessageStatus.SENT
    assert len(push.sent) == 2
    assert await dead_jobs(runtime) == []


async def test_retry_dead_leaves_rows_that_failed_for_other_reasons(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_template(client)
    await create_recipient(client, addresses=[{"channel": "push", "value": "token-1"}])
    push.fail("token-1")
    message = (await client.post("/v1/messages", json=SEND)).json()
    await drain(runtime)
    failed = await load(runtime, Message, message["id"])
    assert failed.status is MessageStatus.FAILED and failed.status_reason == "push: boom"

    async with runtime.transaction() as session:
        await queue.enqueue(session, DELIVER_JOB, {"message_id": message["id"]})
        await session.flush()
        await session.execute(update(Job).values(dead=True, last_error="RuntimeError: other"))
    assert await revive(runtime) == queue.Revival(jobs=1, rows=0)
    await drain(runtime)
    unchanged = await load(runtime, Message, message["id"])
    assert (unchanged.status, unchanged.status_reason) == (MessageStatus.FAILED, "push: boom")
    async with runtime.session() as session:
        assert await session.scalar(select(func.count(Job.id))) == 0


async def test_retry_dead_reopens_broadcasts(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    runtime.settings.worker.max_attempts = 1
    await create_template(client, key="promo", locales={"en": {"body": "Hi"}})
    await create_recipient(client)
    broadcast = (
        await client.post(
            "/v1/broadcasts",
            json={
                "name": "b",
                "template": "promo",
                "category": "transactional",
                "channels": ["push"],
                "audience": {"type": "all"},
            },
        )
    ).json()
    await client.post(f"/v1/broadcasts/{broadcast['id']}/start", json={})
    await failing_worker(runtime, broadcasts.BATCH_JOB).run_until_idle()
    assert (await load(runtime, Broadcast, broadcast["id"])).status is BroadcastStatus.FAILED

    assert await revive(runtime) == queue.Revival(jobs=1, rows=1)
    reopened = await load(runtime, Broadcast, broadcast["id"])
    assert (reopened.status, reopened.error) == (BroadcastStatus.SCHEDULED, None)
    await drain(runtime)
    assert (await load(runtime, Broadcast, broadcast["id"])).status is BroadcastStatus.COMPLETED
    assert len(push.sent) == 1


async def open_digest(client: httpx.AsyncClient, runtime: Runtime) -> Digest:
    assert (await client.post("/v1/templates", json=DIGEST_TEMPLATE)).status_code == 201
    await create_rule(
        client,
        key="comment-digest",
        event="comment.created",
        template="comments",
        channels=["push"],
        digest={"window": "PT10M"},
    )
    await create_recipient(client)
    await send_event(client, "comment.created")
    await drain(runtime)
    async with runtime.session() as session:
        digest = await session.scalar(select(Digest))
    assert digest is not None and digest.status is DigestStatus.OPEN
    return digest


async def flush_now(runtime: Runtime) -> None:
    async with runtime.transaction() as session:
        await session.execute(
            update(Job).where(Job.kind == digests.FLUSH_JOB).values(run_at=datetime.now(UTC))
        )


async def test_retry_dead_reopens_digests_and_their_flush_job(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    digest = await open_digest(client, runtime)
    async with runtime.transaction() as session:
        await session.execute(update(Job).values(max_attempts=1))
    await flush_now(runtime)
    await failing_worker(runtime, digests.FLUSH_JOB).run_until_idle()
    assert (await load(runtime, Digest, digest.id)).status is DigestStatus.CANCELLED

    assert await revive(runtime) == queue.Revival(jobs=1, rows=1)
    assert (await load(runtime, Digest, digest.id)).status is DigestStatus.OPEN
    async with runtime.session() as session:
        job = await session.scalar(select(Job).where(Job.kind == digests.FLUSH_JOB))
    assert job is not None and job.key == f"{digests.FLUSH_JOB}:{digest.id}"

    await drain(runtime)
    sent = await load(runtime, Digest, digest.id)
    assert sent.status is DigestStatus.SENT and sent.message_id is not None
    assert len(push.sent) == 1


async def test_retry_dead_keeps_a_digest_closed_once_a_newer_one_is_open(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    digest = await open_digest(client, runtime)
    async with runtime.transaction() as session:
        await session.execute(update(Job).values(max_attempts=1))
    await flush_now(runtime)
    await failing_worker(runtime, digests.FLUSH_JOB).run_until_idle()
    await send_event(client, "comment.created")
    await drain(runtime)

    assert await revive(runtime) == queue.Revival(jobs=1, rows=0)
    assert (await load(runtime, Digest, digest.id)).status is DigestStatus.CANCELLED
    async with runtime.session() as session:
        statuses = (await session.scalars(select(Digest.status))).all()
    assert sorted(statuses) == [DigestStatus.CANCELLED, DigestStatus.OPEN]


@postgres_only
async def test_digest_flush_locks_the_recipient_before_the_digest(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    digest = await open_digest(client, runtime)
    context = queue.JobContext(runtime, {"digest_id": str(digest.id)}, 1, 1)

    async def add_holding_the_recipient(session: AsyncSession) -> None:
        await session.execute(
            select(Recipient.id).where(Recipient.id == digest.recipient_id).with_for_update()
        )
        flushing = asyncio.create_task(digests.flush(context))
        await asyncio.sleep(0.5)
        assert not flushing.done()
        await session.execute(
            select(Digest.id).where(Digest.id == digest.id).with_for_update(nowait=True)
        )
        await session.execute(
            update(Digest).where(Digest.id == digest.id).values(count=Digest.count + 1)
        )
        await session.commit()
        await asyncio.wait_for(flushing, 10)

    async with runtime.session() as session:
        await add_holding_the_recipient(session)
    flushed = await load(runtime, Digest, digest.id)
    assert flushed.status is DigestStatus.SENT and flushed.count == 2
