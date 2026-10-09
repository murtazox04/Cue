from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from cue_notify import queue
from cue_notify.db.models import Job
from cue_notify.runtime import Runtime


async def _jobs(runtime: Runtime) -> list[Job]:
    async with runtime.session() as session:
        return list((await session.scalars(select(Job))).all())


async def test_keyed_enqueue_is_idempotent(runtime: Runtime) -> None:
    async with runtime.transaction() as session:
        await queue.enqueue(session, "noop", key="k")
        await queue.enqueue(session, "noop", key="k")
        await queue.enqueue(session, "noop")
    async with runtime.session() as session:
        assert await session.scalar(select(func.count(Job.id))) == 2


async def test_success_retry_and_dead_letter(runtime: Runtime) -> None:
    calls: list[str] = []

    async def ok(ctx: queue.JobContext) -> None:
        calls.append(ctx.payload["name"])

    async def flaky(ctx: queue.JobContext) -> None:
        raise RuntimeError(f"attempt {ctx.attempt}")

    async def fatal(_: queue.JobContext) -> None:
        raise queue.PermanentJobError("no point retrying")

    async def later(_: queue.JobContext) -> None:
        raise queue.RetryLater(120, "rate limited")

    async with runtime.transaction() as session:
        await queue.enqueue(session, "ok", {"name": "a"})
        await queue.enqueue(session, "flaky", max_attempts=2)
        await queue.enqueue(session, "fatal")
        await queue.enqueue(session, "later")
        await queue.enqueue(session, "unknown")
        await queue.enqueue(
            session, "ok", {"name": "future"}, run_at=datetime.now(UTC) + timedelta(hours=1)
        )

    worker = queue.Worker(runtime, {"ok": ok, "flaky": flaky, "fatal": fatal, "later": later})
    await worker.run_until_idle()
    assert calls == ["a"]

    jobs = {job.kind: job for job in await _jobs(runtime)}
    assert "ok" in jobs and jobs["ok"].payload == {"name": "future"}  # not due, untouched
    assert jobs["flaky"].attempts == 1 and not jobs["flaky"].dead
    assert jobs["flaky"].last_error == "RuntimeError: attempt 1"
    assert jobs["fatal"].dead and jobs["fatal"].last_error == "no point retrying"
    assert jobs["unknown"].dead and "no handler" in str(jobs["unknown"].last_error)
    assert jobs["later"].run_at > datetime.now(UTC) + timedelta(seconds=100)

    # Make the flaky job due again: its second (final) failure buries it.
    async with runtime.transaction() as session:
        stored = await session.get(Job, jobs["flaky"].id)
        assert stored is not None
        stored.run_at = datetime.now(UTC)
    await worker.run_until_idle()
    async with runtime.session() as session:
        buried = await session.get(Job, jobs["flaky"].id)
    assert buried is not None and buried.dead and buried.attempts == 2


async def test_leased_jobs_are_not_claimed_twice(runtime: Runtime) -> None:
    async with runtime.transaction() as session:
        await queue.enqueue(session, "noop")
    first = queue.Worker(runtime, {}, name="a")
    second = queue.Worker(runtime, {}, name="b")
    assert len(await first._claim(10)) == 1
    assert await second._claim(10) == []


@pytest.mark.parametrize("attempt", [1, 2, 5, 20])
def test_backoff_grows_and_is_capped(attempt: int) -> None:
    delay = queue.backoff(attempt, base=5, ceiling=60)
    assert 0 < delay <= 60
    assert delay >= min(60, 5 * 2 ** (attempt - 1)) * 0.5
