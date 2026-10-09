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
import os
import random
import socket
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.core.ids import uuid7
from cue_notify.core.time import utcnow
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


def backoff(attempt: int, base: float, ceiling: float) -> float:
    """Exponential backoff with jitter: ~base, ~2·base, ~4·base… capped at ``ceiling``."""
    exponential = min(ceiling, base * 2 ** max(attempt - 1, 0))
    return exponential * random.uniform(0.5, 1.0)  # noqa: S311 — jitter, not crypto


class Worker:
    def __init__(
        self,
        runtime: Runtime,
        handlers: Mapping[str, Handler],
        *,
        name: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.handlers = dict(handlers)
        self.settings = runtime.settings.worker
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self._slots = asyncio.Semaphore(self.settings.concurrency)
        self._tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ public API

    async def run(self, stop: asyncio.Event) -> None:
        """Process jobs until ``stop`` is set, then drain in-flight jobs."""
        logger.info("worker started", extra={"worker": self.name})
        try:
            while not stop.is_set():
                claimed = await self._claim_and_spawn()
                if not claimed:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), self.settings.poll_interval)
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
            task.add_done_callback(self._tasks.discard)
        return len(jobs)

    async def _claim(self, limit: int) -> list[Job]:
        now = utcnow()
        lease_until = now + timedelta(seconds=self.settings.lease_seconds)
        available = or_(Job.locked_until.is_(None), Job.locked_until < now)
        async with self.runtime.session() as session, session.begin():
            candidates = (
                await session.scalars(
                    select(Job.id)
                    .where(Job.dead.is_(False), Job.run_at <= now, available)
                    .order_by(Job.run_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            claimed: list[Job] = []
            for job_id in candidates:
                # The conditional update is the lease: it only succeeds for one worker.
                result = await session.execute(
                    update(Job)
                    .where(Job.id == job_id, available)
                    .values(
                        locked_until=lease_until, locked_by=self.name, attempts=Job.attempts + 1
                    )
                    .returning(Job)
                )
                job = result.scalar_one_or_none()
                if job is not None:
                    claimed.append(job)
            return claimed

    async def _execute(self, job: Job) -> None:
        handler = self.handlers.get(job.kind)
        log = {"job_id": str(job.id), "kind": job.kind, "attempt": job.attempts}
        started = time.perf_counter()
        outcome = "retried"
        try:
            if handler is None:
                raise PermanentJobError(f"no handler registered for job kind {job.kind!r}")
            await handler(JobContext(self.runtime, job.payload, job.attempts, job.max_attempts))
        except RetryLater as exc:
            if job.attempts >= job.max_attempts:
                logger.error("job gave up after %d attempts: %s", job.attempts, exc, extra=log)
                outcome = "dead"
                await self._bury(job, str(exc))
            else:
                logger.info("job deferred: %s", exc, extra=log)
                await self._reschedule(job, delay=exc.delay, error=str(exc))
        except PermanentJobError as exc:
            logger.error("job failed permanently: %s", exc, extra=log)
            outcome = "dead"
            await self._bury(job, str(exc))
        except Exception as exc:
            if job.attempts >= job.max_attempts:
                logger.exception("job failed after %d attempts", job.attempts, extra=log)
                outcome = "dead"
                await self._bury(job, f"{type(exc).__name__}: {exc}")
            else:
                delay = backoff(
                    job.attempts, self.settings.retry_base_seconds, self.settings.retry_max_seconds
                )
                logger.warning("job failed, retrying in %.0fs: %s", delay, exc, extra=log)
                await self._reschedule(job, delay=delay, error=f"{type(exc).__name__}: {exc}")
        else:
            async with self.runtime.session() as session, session.begin():
                await session.execute(delete(Job).where(Job.id == job.id))
            outcome = "done"
            logger.debug("job done", extra=log)
        finally:
            self.runtime.metrics.job_finished(job.kind, outcome, time.perf_counter() - started)

    async def _reschedule(self, job: Job, *, delay: float, error: str) -> None:
        values: dict[str, Any] = {
            "run_at": utcnow() + timedelta(seconds=delay),
            "locked_until": None,
            "locked_by": None,
            "last_error": error[:4000],
        }
        async with self.runtime.session() as session, session.begin():
            await session.execute(update(Job).where(Job.id == job.id).values(values))

    async def _bury(self, job: Job, error: str) -> None:
        async with self.runtime.session() as session, session.begin():
            await session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(dead=True, locked_until=None, locked_by=None, last_error=error[:4000])
            )

    async def _drain(self) -> None:
        if not self._tasks:
            return
        _, pending = await asyncio.wait(self._tasks, timeout=self.settings.shutdown_timeout)
        for task in pending:  # leases expire, so another worker will pick these up
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
