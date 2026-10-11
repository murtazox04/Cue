"""A durable job queue backed by the application database.

Why not Redis/Celery? Jobs are enqueued in the *same transaction* as the rows they refer
to (the transactional-outbox pattern): an event is never stored without its processing
job, and a message is never queued without its delivery job. Delayed jobs are just rows
with a future ``run_at``, so scheduled sends survive restarts with no extra scheduler.

Workers claim jobs with ``SELECT … FOR UPDATE SKIP LOCKED`` on PostgreSQL plus a
conditional update that doubles as a lease, so any number of workers can run safely.
Delivery is at-least-once: handlers must be idempotent (all of Cue's are).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import random
import socket
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import ColumnElement, delete, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.core.ids import token as lease_token
from cue_notify.core.ids import uuid7
from cue_notify.core.time import utcnow
from cue_notify.db.base import UTCDateTime
from cue_notify.db.dml import insert_ignore
from cue_notify.db.models import Job

if TYPE_CHECKING:
    from cue_notify.runtime import Runtime

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JobContext:
    runtime: Runtime
    payload: dict[str, Any]
    attempt: int
    max_attempts: int

    @property
    def is_last_attempt(self) -> bool:
        return self.attempt >= self.max_attempts


type Handler = Callable[[JobContext], Awaitable[None]]


class PermanentJobError(Exception):
    """Raise from a handler to fail a job immediately without further retries."""


class RetryLater(Exception):  # noqa: N818 — reads naturally at the raise site
    """Raise from a handler to retry after a specific delay (e.g. a provider's Retry-After)."""

    def __init__(self, delay: float, reason: str = "") -> None:
        super().__init__(reason or f"retry in {delay:.0f}s")
        self.delay = delay


class JobTimeoutError(Exception):
    pass


class LeaseLostError(Exception):
    pass


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    run_at: datetime | None = None,
    key: str | None = None,
    max_attempts: int = 10,
) -> None:
    """Add a job within the caller's transaction.

    ``key`` makes the enqueue idempotent: a second job with the same key is ignored while
    the first one still exists.
    """
    values: dict[str, Any] = {
        "kind": kind,
        "payload": dict(payload or {}),
        "run_at": run_at or utcnow(),
        "key": key,
        "max_attempts": max_attempts,
        "created_at": utcnow(),
    }
    if key is None:
        session.add(Job(**values))
    else:
        await insert_ignore(session, Job, {"id": uuid7(), **values}, conflict=["key"])


async def reschedule(session: AsyncSession, key: str, run_at: datetime) -> None:
    """Move a pending keyed job to a new time (no-op if it is running or gone)."""
    await session.execute(
        update(Job)
        .where(Job.key == key, Job.dead.is_(False), Job.locked_until.is_(None))
        .values(run_at=run_at)
    )


def backoff(attempt: int, base: float, ceiling: float) -> float:
    """Exponential backoff with jitter: ~base, ~2·base, ~4·base… capped at ``ceiling``."""
    exponential = min(ceiling, base * 2 ** max(attempt - 1, 0))
    return exponential * random.uniform(0.5, 1.0)  # noqa: S311 — jitter, not crypto


type DeadHandler = Callable[[JobContext, str], Awaitable[None]]


def database_clock(session: AsyncSession, offset: timedelta | None = None) -> ColumnElement[Any]:
    if session.get_bind().dialect.name == "postgresql":
        return func.now() if offset is None else func.now() + offset
    return literal(utcnow() + (offset or timedelta()), UTCDateTime())


class Worker:
    """Runs jobs. ``on_dead`` hooks let a job kind record a final failure on its own rows."""

    def __init__(
        self,
        runtime: Runtime,
        handlers: Mapping[str, Handler],
        *,
        on_dead: Mapping[str, DeadHandler] | None = None,
        name: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.handlers = dict(handlers)
        self.on_dead = dict(on_dead or {})
        self.settings = runtime.settings.worker
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ public API

    async def run(self, stop: asyncio.Event) -> None:
        """Process jobs until ``stop`` is set, then drain in-flight jobs.

        Database outages do not end the loop: claiming backs off and retries.
        """
        logger.info("worker started", extra={"worker": self.name})
        failures = 0
        try:
            while not stop.is_set():
                try:
                    claimed = await self._claim_and_spawn()
                    failures = 0
                except Exception:
                    failures += 1
                    claimed = 0
                    logger.exception("claiming jobs failed (%d in a row)", failures)
                wait = self.settings.poll_interval if claimed == 0 else 0
                if failures:
                    wait = backoff(failures, self.settings.poll_interval, 30.0)
                if wait:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), wait)
        finally:
            await self._drain()
            logger.info("worker stopped", extra={"worker": self.name})

    async def run_until_idle(self, *, max_rounds: int = 10_000) -> int:
        """Run every job that is due right now (including ones they enqueue). Returns count.

        Useful for tests, one-off CLI runs and cron-style deployments.
        """
        processed = 0
        for _ in range(max_rounds):
            jobs = await self._claim(self.settings.concurrency)
            if not jobs:
                break
            await asyncio.gather(*(self._execute(job) for job in jobs))
            processed += len(jobs)
        return processed

    # ------------------------------------------------------------------ internals

    async def _claim_and_spawn(self) -> int:
        free = self.settings.concurrency - len(self._tasks)
        if free <= 0:
            await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
            return 1
        jobs = await self._claim(free)
        for job in jobs:
            task = asyncio.create_task(self._execute(job), name=f"job:{job.kind}:{job.id}")
            self._tasks.add(task)
            task.add_done_callback(self.forget)
        return len(jobs)

    def forget(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "job task crashed", exc_info=task.exception(), extra={"task": task.get_name()}
            )

    async def _claim(self, limit: int) -> list[Job]:
        now = utcnow()
        claimed: list[Job] = []
        abandoned: list[Job] = []
        async with self.runtime.session() as session, session.begin():
            lease_until = database_clock(session, timedelta(seconds=self.settings.lease_seconds))
            available = or_(Job.locked_until.is_(None), Job.locked_until < database_clock(session))
            candidates = (
                await session.execute(
                    select(Job.id, Job.attempts, Job.max_attempts)
                    .where(Job.dead.is_(False), Job.run_at <= now, available)
                    .order_by(Job.run_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for job_id, attempts, max_attempts in candidates:
                # The conditional update is the lease: it only succeeds for one worker. The
                # lease token identifies this claim, so a worker that overran its lease can
                # never complete or reschedule a job another worker has since claimed.
                exhausted = attempts >= max_attempts
                values: dict[str, Any] = {
                    "locked_until": lease_until,
                    "locked_by": f"{self.name}:{lease_token(9)}",
                }
                if not exhausted:
                    values["attempts"] = Job.attempts + 1
                result = await session.execute(
                    update(Job).where(Job.id == job_id, available).values(values).returning(Job)
                )
                job = result.scalar_one_or_none()
                if job is not None:
                    (abandoned if exhausted else claimed).append(job)
        for job in abandoned:
            await self.abandon(job)
        return claimed

    async def abandon(self, job: Job) -> None:
        log = {"job_id": str(job.id), "kind": job.kind, "attempt": job.attempts}
        error = (
            f"abandoned after {job.attempts} attempts: the worker running it stopped "
            "or lost its lease every time"
        )
        logger.error("job %s", error, extra=log)
        context = JobContext(self.runtime, job.payload, job.attempts, job.max_attempts)
        try:
            await self._bury(job, context, error)
        except Exception:
            logger.exception(
                "burying an abandoned job failed; it is retried once its lease expires",
                extra=log,
            )
            return
        self.runtime.metrics.job_finished(job.kind, "dead", 0.0)

    async def _heartbeat(self, job: Job, work: asyncio.Task[None]) -> None:
        """Extend the lease while the handler runs, so long jobs are not run twice."""
        interval = self.settings.lease_seconds / 3
        log = {"job_id": str(job.id), "kind": job.kind, "attempt": job.attempts}
        while True:
            await asyncio.sleep(interval)
            try:
                async with self.runtime.session() as session, session.begin():
                    lease_until = database_clock(
                        session, timedelta(seconds=self.settings.lease_seconds)
                    )
                    result = await session.execute(
                        update(Job).where(*self._owned(job)).values(locked_until=lease_until)
                    )
                    renewed = int(getattr(result, "rowcount", 0))
            except Exception:
                logger.warning("lease heartbeat failed", exc_info=True, extra=log)
                continue
            if renewed == 0:
                logger.error("job lost its lease to another worker; cancelling it", extra=log)
                work.cancel()
                return

    async def _execute(self, job: Job) -> None:
        log = {"job_id": str(job.id), "kind": job.kind, "attempt": job.attempts}
        context = JobContext(self.runtime, job.payload, job.attempts, job.max_attempts)
        started = time.perf_counter()
        outcome = "lost"
        work = asyncio.create_task(self.invoke(job, context), name=f"handler:{job.kind}:{job.id}")
        heartbeat = asyncio.create_task(self._heartbeat(job, work))
        try:
            try:
                failure = await self.outcome_of(work)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
            if not isinstance(failure, LeaseLostError):
                outcome = await self.conclude(job, context, failure, log)
        except asyncio.CancelledError:
            outcome = "interrupted"
            await asyncio.wait({work})
            await asyncio.shield(self.release(job, log))
            raise
        finally:
            self.runtime.metrics.job_finished(job.kind, outcome, time.perf_counter() - started)

    async def invoke(self, job: Job, context: JobContext) -> None:
        handler = self.handlers.get(job.kind)
        if handler is None:
            raise PermanentJobError(f"no handler registered for job kind {job.kind!r}")
        deadline = asyncio.timeout(self.settings.job_timeout)
        try:
            async with deadline:
                await handler(context)
        except TimeoutError as exc:
            if deadline.expired():
                raise JobTimeoutError(
                    f"job ran longer than {self.settings.job_timeout:g}s"
                ) from exc
            raise

    @staticmethod
    async def outcome_of(work: asyncio.Task[None]) -> Exception | None:
        try:
            await work
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return LeaseLostError("another worker took over the job's lease")
        except Exception as exc:
            return exc
        return None

    async def conclude(
        self, job: Job, context: JobContext, failure: Exception | None, log: dict[str, Any]
    ) -> str:
        try:
            return await self.record(job, context, failure, log)
        except Exception:
            logger.exception(
                "recording the job's outcome failed; it runs again once its lease expires",
                extra=log,
            )
            return "unrecorded"

    async def record(
        self, job: Job, context: JobContext, failure: Exception | None, log: dict[str, Any]
    ) -> str:
        if failure is None:
            async with self.runtime.session() as session, session.begin():
                await session.execute(delete(Job).where(*self._owned(job)))
            logger.debug("job done", extra=log)
            return "done"
        if isinstance(failure, RetryLater):
            return await self.defer(job, context, failure, log)
        if isinstance(failure, PermanentJobError):
            logger.error("job failed permanently: %s", failure, extra=log)
            await self._bury(job, context, str(failure))
            return "dead"
        error = f"{type(failure).__name__}: {failure}"
        if job.attempts >= job.max_attempts:
            logger.error("job failed after %d attempts", job.attempts, exc_info=failure, extra=log)
            await self._bury(job, context, error)
            return "dead"
        delay = backoff(
            job.attempts, self.settings.retry_base_seconds, self.settings.retry_max_seconds
        )
        logger.warning("job failed, retrying in %.0fs: %s", delay, failure, extra=log)
        await self._reschedule(job, delay=delay, error=error)
        return "retried"

    async def defer(
        self, job: Job, context: JobContext, request: RetryLater, log: dict[str, Any]
    ) -> str:
        now = utcnow()
        since = job.deferred_since or now
        if now - since >= timedelta(seconds=self.settings.max_defer_seconds):
            logger.error("job kept deferring since %s: %s", since.isoformat(), request, extra=log)
            await self._bury(job, context, f"deferred since {since.isoformat()}: {request}")
            return "dead"
        ceiling = self.settings.retry_max_seconds
        delay = min(max(request.delay, 0.0), ceiling) if math.isfinite(request.delay) else ceiling
        logger.info("job deferred: %s", request, extra=log)
        await self._reschedule(
            job, delay=delay, error=str(request), refund=True, deferred_since=since
        )
        return "retried"

    @staticmethod
    def _owned(job: Job) -> tuple[Any, ...]:
        return (Job.id == job.id, Job.locked_by == job.locked_by)

    async def _reschedule(
        self,
        job: Job,
        *,
        delay: float,
        error: str,
        refund: bool = False,
        deferred_since: datetime | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "run_at": utcnow() + timedelta(seconds=delay),
            "locked_until": None,
            "locked_by": None,
            "last_error": error[:4000],
        }
        if refund:
            values["attempts"] = Job.attempts - 1
        if deferred_since is not None:
            values["deferred_since"] = deferred_since
        async with self.runtime.session() as session, session.begin():
            await session.execute(update(Job).where(*self._owned(job)).values(values))

    async def release(self, job: Job, log: dict[str, Any]) -> None:
        try:
            async with self.runtime.session() as session, session.begin():
                await session.execute(
                    update(Job)
                    .where(*self._owned(job))
                    .values(locked_until=None, locked_by=None, attempts=Job.attempts - 1)
                )
        except Exception:
            logger.exception(
                "releasing an interrupted job failed; it runs again once its lease expires",
                extra=log,
            )
            return
        logger.info("interrupted job released for another worker", extra=log)

    async def _bury(self, job: Job, context: JobContext, error: str) -> None:
        hook = self.on_dead.get(job.kind)
        if hook is not None:
            try:
                await hook(context, error)
            except Exception:
                logger.exception("on-dead hook failed", extra={"job_id": str(job.id)})
        async with self.runtime.session() as session, session.begin():
            # Releasing the key lets the same work be enqueued again once the cause is fixed.
            await session.execute(
                update(Job)
                .where(*self._owned(job))
                .values(
                    dead=True, key=None, locked_until=None, locked_by=None, last_error=error[:4000]
                )
            )

    async def _drain(self) -> None:
        if not self._tasks:
            return
        _, pending = await asyncio.wait(self._tasks, timeout=self.settings.shutdown_timeout)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
