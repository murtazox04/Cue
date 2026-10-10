"""Registry of background job handlers."""

from __future__ import annotations

from cue_notify.queue import DeadHandler, Handler, Worker
from cue_notify.runtime import Runtime
from cue_notify.services import agents, broadcasts, delivery, digests, events
from cue_notify.services.composer import DELIVER_JOB, REVIEW_JOB

HANDLERS: dict[str, Handler] = {
    events.PROCESS_JOB: events.process,
    DELIVER_JOB: delivery.deliver,
    broadcasts.BATCH_JOB: broadcasts.run_batch,
    digests.FLUSH_JOB: digests.flush,
    REVIEW_JOB: agents.request_review,
}

ON_DEAD: dict[str, DeadHandler] = {
    events.PROCESS_JOB: events.process_dead,
    DELIVER_JOB: delivery.deliver_dead,
    broadcasts.BATCH_JOB: broadcasts.run_batch_dead,
    digests.FLUSH_JOB: digests.flush_dead,
}


def create_worker(runtime: Runtime, name: str | None = None) -> Worker:
    return Worker(runtime, HANDLERS, on_dead=ON_DEAD, name=name)
