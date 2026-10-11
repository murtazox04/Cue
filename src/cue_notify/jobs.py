"""Registry of background job handlers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.queue import DeadHandler, Handler, Revival, ReviveHandler, Worker, revive_dead
from cue_notify.runtime import Runtime
from cue_notify.services import agents, approvals, broadcasts, delivery, digests, events
from cue_notify.services.composer import DELIVER_JOB, REVIEW_JOB

HANDLERS: dict[str, Handler] = {
    events.PROCESS_JOB: events.process,
    DELIVER_JOB: delivery.deliver,
    broadcasts.BATCH_JOB: broadcasts.run_batch,
    digests.FLUSH_JOB: digests.flush,
    REVIEW_JOB: agents.request_review,
    approvals.REQUEST_JOB: approvals.request,
    approvals.EXPIRE_JOB: approvals.expire,
    approvals.CALLBACK_JOB: approvals.callback,
}

ON_DEAD: dict[str, DeadHandler] = {
    events.PROCESS_JOB: events.process_dead,
    DELIVER_JOB: delivery.deliver_dead,
    broadcasts.BATCH_JOB: broadcasts.run_batch_dead,
    digests.FLUSH_JOB: digests.flush_dead,
    REVIEW_JOB: approvals.review_dead,
    approvals.REQUEST_JOB: approvals.request_dead,
    approvals.EXPIRE_JOB: approvals.expire_dead,
    approvals.CALLBACK_JOB: approvals.callback_dead,
}

ON_REVIVE: dict[str, ReviveHandler] = {
    events.PROCESS_JOB: events.process_revive,
    DELIVER_JOB: delivery.deliver_revive,
    broadcasts.BATCH_JOB: broadcasts.run_batch_revive,
    digests.FLUSH_JOB: digests.flush_revive,
}


def create_worker(runtime: Runtime, name: str | None = None) -> Worker:
    return Worker(runtime, HANDLERS, on_dead=ON_DEAD, name=name)


async def revive(session: AsyncSession, kind: str | None = None) -> Revival:
    return await revive_dead(session, ON_REVIVE, kind=kind)
