from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from cue_notify import queue
from cue_notify.db.models import Job
from cue_notify.runtime import Runtime
from tests.conftest import POSTGRES_URL


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
    runtime.settings.worker.retry_max_seconds = 300
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
    assert jobs["later"].attempts == 0 and jobs["later"].deferred_since is not None

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


async def make_due(runtime: Runtime) -> None:
    async with runtime.transaction() as session:
        await session.execute(update(Job).values(run_at=datetime.now(UTC)))


async def expire_leases(runtime: Runtime) -> None:
    async with runtime.transaction() as session:
        await session.execute(
            update(Job).values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
        )


def dead_recorder(errors: list[str]) -> queue.DeadHandler:
    async def record(_: queue.JobContext, error: str) -> None:
        errors.append(error)

    return record


async def test_handler_timeout_is_a_retryable_failure(runtime: Runtime) -> None:
    runtime.settings.worker.job_timeout = 0.05
    buried: list[str] = []

    async def hang(_: queue.JobContext) -> None:
        await asyncio.sleep(30)

    async with runtime.transaction() as session:
        await queue.enqueue(session, "hang", max_attempts=2)
    worker = queue.Worker(runtime, {"hang": hang}, on_dead={"hang": dead_recorder(buried)})

    await worker.run_until_idle()
    [job] = await _jobs(runtime)
    assert (job.dead, job.attempts, job.locked_by) == (False, 1, None)
    assert job.last_error == "JobTimeoutError: job ran longer than 0.05s"

    await make_due(runtime)
    await worker.run_until_idle()
    [job] = await _jobs(runtime)
    assert job.dead and buried == [job.last_error]


async def test_job_that_keeps_killing_its_worker_is_buried_when_claimed(
    runtime: Runtime,
) -> None:
    calls: list[int] = []
    buried: list[str] = []

    async def crashy(ctx: queue.JobContext) -> None:
        calls.append(ctx.attempt)

    async with runtime.transaction() as session:
        await queue.enqueue(session, "crashy", max_attempts=3)
    crashed = queue.Worker(runtime, {}, name="crashed")
    for _ in range(3):
        assert len(await crashed._claim(1)) == 1
        await expire_leases(runtime)

    survivor = queue.Worker(runtime, {"crashy": crashy}, on_dead={"crashy": dead_recorder(buried)})
    assert await survivor.run_until_idle() == 0
    [job] = await _jobs(runtime)
    assert job.dead and job.attempts == 3 and job.locked_by is None
    assert job.last_error is not None and job.last_error.startswith("abandoned after 3 attempts")
    assert calls == [] and buried == [job.last_error]


async def test_lost_lease_cancels_the_handler(
    runtime: Runtime, caplog: pytest.LogCaptureFixture
) -> None:
    runtime.settings.worker.lease_seconds = 1
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking(_: queue.JobContext) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async with runtime.transaction() as session:
        await queue.enqueue(session, "blocking")
    worker = queue.Worker(runtime, {"blocking": blocking}, name="a")
    [job] = await worker._claim(1)
    with caplog.at_level(logging.ERROR, logger="cue_notify.queue"):
        execution = asyncio.create_task(worker._execute(job))
        await asyncio.wait_for(started.wait(), 5)
        async with runtime.transaction() as session:
            await session.execute(update(Job).values(locked_by="another-worker"))
        await asyncio.wait_for(execution, 5)

    assert cancelled.is_set()
    [stored] = await _jobs(runtime)
    assert stored.locked_by == "another-worker" and not stored.dead
    lost = [r for r in caplog.records if "lost its lease" in r.getMessage()]
    assert [getattr(r, "job_id", None) for r in lost] == [str(job.id)]


async def test_bookkeeping_failures_are_logged_and_left_to_lease_expiry(
    runtime: Runtime, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def flaky(_: queue.JobContext) -> None:
        raise RuntimeError("boom")

    async def unreachable(*_: object, **__: object) -> None:
        raise ConnectionError("database went away")

    async with runtime.transaction() as session:
        await queue.enqueue(session, "flaky")
    worker = queue.Worker(runtime, {"flaky": flaky})
    monkeypatch.setattr(worker, "_reschedule", unreachable)
    [job] = await worker._claim(1)
    with caplog.at_level(logging.ERROR, logger="cue_notify.queue"):
        await worker._execute(job)

    [record] = [r for r in caplog.records if "recording the job's outcome failed" in r.getMessage()]
    assert (getattr(record, "job_id", None), getattr(record, "kind", None)) == (
        str(job.id),
        "flaky",
    )
    [stored] = await _jobs(runtime)
    assert stored.locked_by == job.locked_by and stored.locked_until is not None


async def test_deferrals_do_not_use_up_attempts_but_are_bounded_in_time(
    runtime: Runtime,
) -> None:
    buried: list[str] = []

    async def busy(_: queue.JobContext) -> None:
        raise queue.RetryLater(60, "rate limited")

    async with runtime.transaction() as session:
        await queue.enqueue(session, "busy", max_attempts=2)
    worker = queue.Worker(runtime, {"busy": busy}, on_dead={"busy": dead_recorder(buried)})
    for _ in range(5):
        await worker.run_until_idle()
        [job] = await _jobs(runtime)
        assert (job.attempts, job.dead, job.last_error) == (0, False, "rate limited")
        await make_due(runtime)
    assert job.deferred_since is not None

    window = timedelta(seconds=runtime.settings.worker.max_defer_seconds)
    async with runtime.transaction() as session:
        await session.execute(update(Job).values(deferred_since=datetime.now(UTC) - window))
    await worker.run_until_idle()
    [job] = await _jobs(runtime)
    assert job.dead and job.last_error is not None and "rate limited" in job.last_error
    assert buried == [job.last_error]


@pytest.mark.parametrize("delay", [math.inf, math.nan, -5.0, 1e12])
async def test_retry_later_delays_are_clamped(runtime: Runtime, delay: float) -> None:
    async def later(_: queue.JobContext) -> None:
        raise queue.RetryLater(delay)

    async with runtime.transaction() as session:
        await queue.enqueue(session, "later")
    before = datetime.now(UTC)
    await queue.Worker(runtime, {"later": later}).run_until_idle(max_rounds=1)
    [job] = await _jobs(runtime)
    ceiling = timedelta(seconds=runtime.settings.worker.retry_max_seconds + 1)
    assert not job.dead and job.locked_by is None
    assert before <= job.run_at <= datetime.now(UTC) + ceiling


async def test_shutdown_releases_interrupted_jobs_at_once(runtime: Runtime) -> None:
    runtime.settings.worker.shutdown_timeout = 0.05
    runtime.settings.worker.poll_interval = 0.01
    started = asyncio.Event()

    async def long(_: queue.JobContext) -> None:
        started.set()
        await asyncio.sleep(30)

    async with runtime.transaction() as session:
        await queue.enqueue(session, "long")
    worker = queue.Worker(runtime, {"long": long}, name="a")
    stop = asyncio.Event()
    running = asyncio.create_task(worker.run(stop))
    await asyncio.wait_for(started.wait(), 5)
    stop.set()
    await asyncio.wait_for(running, 5)

    [job] = await _jobs(runtime)
    assert (job.locked_until, job.locked_by, job.attempts, job.dead) == (None, None, 0, False)
    assert len(await queue.Worker(runtime, {}, name="b")._claim(1)) == 1


async def test_concurrent_workers_bury_an_abandoned_job_once(runtime: Runtime) -> None:
    buried: list[str] = []
    async with runtime.transaction() as session:
        await queue.enqueue(session, "crashy", max_attempts=1)
        await session.execute(
            update(Job).values(attempts=1, locked_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    workers = [
        queue.Worker(runtime, {}, on_dead={"crashy": dead_recorder(buried)}, name=f"w{index}")
        for index in range(4)
    ]
    await asyncio.gather(*(worker.run_until_idle() for worker in workers))
    [job] = await _jobs(runtime)
    assert job.dead and len(buried) == 1


@pytest.mark.skipif(not POSTGRES_URL, reason="needs CUE_TEST_DATABASE_URL")
async def test_leases_follow_the_database_clock(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with runtime.transaction() as session:
        await queue.enqueue(session, "noop")
    [job] = await queue.Worker(runtime, {}, name="a")._claim(1)
    async with runtime.session() as session:
        database_now = await session.scalar(select(func.now()))
    assert database_now is not None and job.locked_until is not None
    lease = timedelta(seconds=runtime.settings.worker.lease_seconds)
    assert abs(job.locked_until - (database_now + lease)) < timedelta(seconds=5)

    skewed = datetime.now(UTC) + timedelta(hours=1)
    monkeypatch.setattr(queue, "utcnow", lambda: skewed)
    assert await queue.Worker(runtime, {}, name="fast-clock")._claim(1) == []
