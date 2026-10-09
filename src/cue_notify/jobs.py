"""Registry of background job handlers."""

from __future__ import annotations

from cue_notify.queue import Handler, Worker
from cue_notify.runtime import Runtime
from cue_notify.services import broadcasts, delivery, events
from cue_notify.services.composer import DELIVER_JOB

HANDLERS: dict[str, Handler] = {
    events.PROCESS_JOB: events.process,
    DELIVER_JOB: delivery.deliver,
    broadcasts.BATCH_JOB: broadcasts.run_batch,
}


def create_worker(runtime: Runtime, name: str | None = None) -> Worker:
    return Worker(runtime, HANDLERS, name=name)
