"""Broadcasts: one template to an audience, processed in resumable batches.

Each batch runs in its own transaction and stores a cursor, and every message carries a
``broadcast:<id>:<recipient>`` dedup key — so pausing, resuming, crashing or retrying a
batch never sends anyone the same broadcast twice. Every recipient still goes through
the composer, so preferences, caps and quiet hours apply to broadcasts too.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify import queue
from cue_notify.core import logic
from cue_notify.core.enums import BroadcastStatus, MessageStatus
from cue_notify.core.time import ensure_utc, utcnow
from cue_notify.db.models import Broadcast, Category, Message, Recipient, Template
from cue_notify.runtime import Runtime
from cue_notify.services import composer
from cue_notify.services import recipients as recipient_service
from cue_notify.services.errors import InvalidStateError, NotFoundError

logger = logging.getLogger(__name__)

BATCH_JOB = "broadcast_batch"
BATCH_SIZE = 500


class AllRecipients(BaseModel):
    type: Literal["all"] = "all"


class RecipientList(BaseModel):
    type: Literal["recipients"] = "recipients"
    external_ids: list[str] = Field(min_length=1, max_length=1_000_000)


class FilteredRecipients(BaseModel):
    """Recipients for whom a JSON Logic ``condition`` over ``{"recipient": …}`` is true."""

    type: Literal["filter"] = "filter"
    condition: dict[str, Any]


type Audience = Annotated[
    AllRecipients | RecipientList | FilteredRecipients, Field(discriminator="type")
]
AudienceAdapter: TypeAdapter[AllRecipients | RecipientList | FilteredRecipients] = TypeAdapter(
    Audience
)


async def get(session: AsyncSession, broadcast_id: UUID) -> Broadcast:
    broadcast = await session.get(Broadcast, broadcast_id)
    if broadcast is None:
        raise NotFoundError("broadcast", broadcast_id)
    return broadcast


async def _schedule_batch(session: AsyncSession, runtime: Runtime, broadcast: Broadcast) -> None:
    await queue.enqueue(
        session,
        BATCH_JOB,
        {"broadcast_id": str(broadcast.id)},
        run_at=broadcast.scheduled_at if broadcast.status is BroadcastStatus.SCHEDULED else None,
        key=f"{BATCH_JOB}:{broadcast.id}:{broadcast.cursor or 'start'}",
        max_attempts=runtime.settings.worker.max_attempts,
    )


async def start(
    session: AsyncSession, runtime: Runtime, broadcast: Broadcast, at: datetime | None = None
) -> Broadcast:
    if broadcast.status not in (BroadcastStatus.DRAFT, BroadcastStatus.SCHEDULED):
        raise InvalidStateError(f"cannot start a broadcast that is {broadcast.status}")
    broadcast.scheduled_at = ensure_utc(at) if at else utcnow()
    broadcast.status = BroadcastStatus.SCHEDULED
    await _schedule_batch(session, runtime, broadcast)
    return broadcast


async def pause(broadcast: Broadcast) -> Broadcast:
    if broadcast.status not in (BroadcastStatus.SCHEDULED, BroadcastStatus.RUNNING):
        raise InvalidStateError(f"cannot pause a broadcast that is {broadcast.status}")
    broadcast.status = BroadcastStatus.PAUSED
    return broadcast


async def resume(session: AsyncSession, runtime: Runtime, broadcast: Broadcast) -> Broadcast:
    if broadcast.status is not BroadcastStatus.PAUSED:
        raise InvalidStateError(f"cannot resume a broadcast that is {broadcast.status}")
    broadcast.status = BroadcastStatus.RUNNING
    await _schedule_batch(session, runtime, broadcast)
    return broadcast


async def cancel(session: AsyncSession, broadcast: Broadcast) -> Broadcast:
    """Stop the broadcast and withdraw every message not sent yet.

    Allowed after completion too: messages may still be waiting (quiet hours, retries).
    """
    if broadcast.status is BroadcastStatus.CANCELLED:
        raise InvalidStateError("broadcast is already cancelled")
    broadcast.status = BroadcastStatus.CANCELLED
    broadcast.completed_at = utcnow()
    await session.execute(
        update(Message)
        .where(Message.broadcast_id == broadcast.id, Message.status == MessageStatus.QUEUED)
        .values(status=MessageStatus.CANCELLED, status_reason="broadcast cancelled")
    )
    return broadcast


async def _next_recipients(
    session: AsyncSession,
    audience: AllRecipients | RecipientList | FilteredRecipients,
    cursor: str | None,
) -> tuple[list[Recipient], str | None]:
    """Return the next batch and the cursor after it (``None`` when exhausted)."""
    if isinstance(audience, RecipientList):
        offset = int(cursor or 0)
        chunk = audience.external_ids[offset : offset + BATCH_SIZE]
        found = (
            await session.scalars(select(Recipient).where(Recipient.external_id.in_(chunk)))
        ).all()
        by_id = {recipient.external_id: recipient for recipient in found}
        batch = [by_id[external_id] for external_id in dict.fromkeys(chunk) if external_id in by_id]
        next_offset = offset + len(chunk)
        return batch, str(next_offset) if next_offset < len(audience.external_ids) else None

    stmt = select(Recipient).order_by(Recipient.id).limit(BATCH_SIZE)
    if cursor:
        stmt = stmt.where(Recipient.id > UUID(cursor))
    page = list((await session.scalars(stmt)).all())
    following = str(page[-1].id) if len(page) == BATCH_SIZE else None
    if isinstance(audience, FilteredRecipients):
        page = [
            recipient
            for recipient in page
            if logic.matches(
                audience.condition, {"recipient": recipient_service.profile(recipient)}
            )
        ]
    return page, following


async def run_batch(ctx: queue.JobContext) -> None:
    """Job handler: process one batch of a broadcast and schedule the next."""
    runtime = ctx.runtime
    broadcast_id = UUID(ctx.payload["broadcast_id"])
    async with runtime.transaction() as session:
        broadcast = await session.get(Broadcast, broadcast_id, with_for_update=True)
        if broadcast is None or broadcast.status not in (
            BroadcastStatus.SCHEDULED,
            BroadcastStatus.RUNNING,
        ):
            return  # paused or cancelled: stop here; resume schedules a fresh batch
        if broadcast.status is BroadcastStatus.SCHEDULED:
            broadcast.status = BroadcastStatus.RUNNING
            broadcast.started_at = utcnow()
        template = await session.scalar(
            select(Template).where(Template.key == broadcast.template_key)
        )
        category = await session.get(Category, broadcast.category_key)
        if template is None or category is None:
            raise queue.PermanentJobError("broadcast template or category was deleted")

        audience = AudienceAdapter.validate_python(broadcast.audience)
        batch, next_cursor = await _next_recipients(session, audience, broadcast.cursor)
        stats = {"targeted": 0, **{status.value: 0 for status in MessageStatus}}
        stats |= {key: int(value) for key, value in broadcast.stats.items()}
        for recipient in batch:
            message, created = await composer.compose(
                session,
                runtime,
                composer.Intent(
                    recipient=recipient,
                    template=template,
                    category=category,
                    channels=list(broadcast.channels),
                    data=broadcast.variables,
                    broadcast_id=broadcast.id,
                    dedup_key=f"broadcast:{broadcast.id}:{recipient.id}",
                ),
            )
            if created:
                stats["targeted"] += 1
                stats[message.status.value] += 1
        broadcast.stats = stats
        broadcast.cursor = next_cursor
        if next_cursor is None:
            broadcast.status = BroadcastStatus.COMPLETED
            broadcast.completed_at = utcnow()
            logger.info("broadcast completed", extra={"broadcast_id": str(broadcast.id), **stats})
        else:
            await _schedule_batch(session, runtime, broadcast)
