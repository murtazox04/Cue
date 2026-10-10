"""Event ingestion and rule evaluation."""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify import queue
from cue_notify.core import logic, policy
from cue_notify.core.enums import EventStatus, MessageStatus
from cue_notify.core.ids import uuid7
from cue_notify.core.time import ensure_utc, utcnow
from cue_notify.db.dml import insert_ignore
from cue_notify.db.models import Category, Event, Recipient, Rule, Template
from cue_notify.runtime import Runtime
from cue_notify.services import composer, digests
from cue_notify.services import recipients as recipient_service
from cue_notify.services.errors import NotFoundError

logger = logging.getLogger(__name__)

PROCESS_JOB = "process_event"


class EventIn(BaseModel):
    name: str = Field(
        min_length=1, max_length=255, pattern=r"^[\w.:\-/]+$", examples=["order.shipped"]
    )
    recipient: str | recipient_service.RecipientIn = Field(
        description="The recipient's external id, or a profile to create/update inline."
    )
    data: dict[str, Any] = Field(default_factory=dict, examples=[{"order_id": "A-1001"}])
    occurred_at: AwareDatetime | None = None
    idempotency_key: str | None = Field(
        default=None,
        max_length=255,
        description="Deduplicates retries from the same source. Strongly recommended.",
    )
    source: str | None = Field(
        default=None,
        max_length=100,
        pattern=r"^[\w.\-:/]+$",
        description="Producing system, scoping idempotency keys. Defaults to the API key name.",
        examples=["billing"],
    )

    @property
    def external_id(self) -> str:
        return self.recipient if isinstance(self.recipient, str) else self.recipient.external_id


async def ingest(
    session: AsyncSession, runtime: Runtime, data: EventIn, *, source: str
) -> tuple[Event, bool]:
    """Store an event and schedule its processing. Returns ``(event, created)``."""
    if isinstance(data.recipient, recipient_service.RecipientIn):
        recipient = await recipient_service.upsert(session, data.recipient)
    else:
        recipient = await recipient_service.ensure(session, data.recipient)

    now = utcnow()
    values: dict[str, Any] = {
        "id": uuid7(),
        "source": source,
        "name": data.name,
        "idempotency_key": data.idempotency_key,
        "recipient_id": recipient.id,
        "data": data.data,
        "occurred_at": ensure_utc(data.occurred_at) if data.occurred_at else now,
        "received_at": now,
        "status": EventStatus.PENDING,
    }
    if data.idempotency_key:
        created = await insert_ignore(
            session, Event, values, conflict=["source", "idempotency_key"]
        )
        event = await session.scalar(
            select(Event).where(
                Event.source == source, Event.idempotency_key == data.idempotency_key
            )
        )
        assert event is not None
    else:
        event = Event(**values)
        session.add(event)
        created = True
    await session.flush()
    if created:
        await queue.enqueue(
            session,
            PROCESS_JOB,
            {"event_id": str(event.id)},
            key=f"{PROCESS_JOB}:{event.id}",
            max_attempts=runtime.settings.worker.max_attempts,
        )
    return event, created


@dataclass(slots=True)
class RuleOutcome:
    rule: str
    matched: bool
    reason: str | None = None
    message_id: str | None = None
    status: str | None = None
    digest_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def evaluation_context(event: Event, recipient: Recipient) -> dict[str, Any]:
    return {
        "event": {
            "name": event.name,
            "source": event.source,
            "data": event.data,
            "occurred_at": event.occurred_at.isoformat(),
        },
        "recipient": recipient_service.profile(recipient),
    }


async def candidate_rules(session: AsyncSession, event_name: str) -> list[Rule]:
    """Enabled rules whose ``event`` pattern matches, highest priority first.

    Patterns support shell-style wildcards (``order.*``). Rule sets are small, so all
    enabled rules are loaded and matched in memory.
    """
    rules = (
        await session.scalars(
            select(Rule).where(Rule.enabled.is_(True)).order_by(Rule.priority.desc(), Rule.key)
        )
    ).all()
    return [rule for rule in rules if fnmatch.fnmatchcase(event_name, rule.event)]


async def evaluate(
    session: AsyncSession,
    runtime: Runtime,
    event: Event,
    recipient: Recipient,
    *,
    dry_run: bool = False,
) -> list[RuleOutcome]:
    context = evaluation_context(event, recipient)
    outcomes: list[RuleOutcome] = []
    for rule in await candidate_rules(session, event.name):
        if not policy.in_rollout(rule.key, recipient.external_id, rule.rollout_percent):
            outcomes.append(RuleOutcome(rule.key, matched=False, reason="outside_rollout"))
            continue
        try:
            matched = logic.matches(rule.condition, context)
        except logic.LogicError as exc:
            outcomes.append(RuleOutcome(rule.key, matched=False, reason=f"condition_error: {exc}"))
            continue
        if not matched:
            outcomes.append(RuleOutcome(rule.key, matched=False, reason="condition_false"))
            continue

        template = await session.scalar(select(Template).where(Template.key == rule.template_key))
        category = await session.get(Category, rule.category_key)
        if template is None or category is None:  # guarded by FKs; defensive for dry runs
            outcomes.append(RuleOutcome(rule.key, matched=True, reason="misconfigured"))
            continue
        if rule.digest is not None:
            if dry_run:
                outcomes.append(RuleOutcome(rule.key, matched=True, reason="digested"))
            else:
                digest = await digests.add(session, runtime, rule, event, recipient, context)
                outcomes.append(
                    RuleOutcome(rule.key, matched=True, reason="digested", digest_id=str(digest.id))
                )
            if rule.stop_processing:
                break
            continue
        intent = composer.Intent(
            recipient=recipient,
            template=template,
            category=category,
            channels=list(rule.channels),
            data=event.data,
            event=event,
            rule=rule,
            delay=timedelta(seconds=rule.delay_seconds),
            cooldown=timedelta(seconds=rule.cooldown_seconds) if rule.cooldown_seconds else None,
            dedup_key=f"event:{event.id}:{rule.id}",
        )
        if dry_run:
            decision = await composer.decide(session, runtime, intent)
            outcomes.append(
                RuleOutcome(rule.key, matched=True, reason=decision.reason, status=decision.status)
            )
        else:
            message, _ = await composer.compose(session, runtime, intent)
            outcomes.append(
                RuleOutcome(
                    rule.key,
                    matched=True,
                    reason=message.status_reason,
                    status=message.status,
                    message_id=str(message.id),
                )
            )
        if rule.stop_processing:
            break
    return outcomes


async def process(ctx: queue.JobContext) -> None:
    """Job handler: evaluate rules for a stored event."""
    event_id = UUID(ctx.payload["event_id"])
    async with ctx.runtime.transaction() as session:
        event = await session.get(Event, event_id, with_for_update=True)
        if event is None:
            raise queue.PermanentJobError(f"event {event_id} not found")
        if event.status is not EventStatus.PENDING:
            return
        recipient = await session.get(Recipient, event.recipient_id) if event.recipient_id else None
        if recipient is None:
            event.status = EventStatus.FAILED
            event.error = "recipient no longer exists"
            return
        outcomes = await evaluate(session, ctx.runtime, event, recipient)
        event.outcome = [outcome.as_dict() for outcome in outcomes]
        event.status = EventStatus.PROCESSED
    queued = sum(1 for outcome in outcomes if outcome.status == MessageStatus.QUEUED)
    logger.info(
        "event processed",
        extra={"event_id": str(event_id), "event": event.name, "queued": queued},
    )


async def get(session: AsyncSession, event_id: UUID) -> Event:
    event = await session.get(Event, event_id)
    if event is None:
        raise NotFoundError("event", event_id)
    return event


async def process_dead(ctx: queue.JobContext, error: str) -> None:
    """Retries exhausted: record why, so the event no longer looks pending."""
    async with ctx.runtime.transaction() as session:
        event = await session.get(Event, UUID(ctx.payload["event_id"]))
        if event is not None and event.status is EventStatus.PENDING:
            event.status = EventStatus.FAILED
            event.error = error[:4000]
