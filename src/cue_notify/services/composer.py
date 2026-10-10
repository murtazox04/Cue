"""Turns an *intent to notify* into a queued (or suppressed) message.

Every path that sends something — event rules, the direct send API and broadcasts —
goes through :func:`compose`, so policy (preferences, reachability, cooldowns,
frequency caps, quiet hours) is applied uniformly and every decision is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify import queue
from cue_notify.core import policy
from cue_notify.core.content import DEFAULT_VARIANT, LocalizedTemplate
from cue_notify.core.enums import COUNTED_STATUSES, MessageStatus, SuppressionReason
from cue_notify.core.policy import FrequencyCap
from cue_notify.core.templating import RenderError
from cue_notify.core.time import utcnow
from cue_notify.db.models import Category, Event, Message, Recipient, Rule, Template
from cue_notify.runtime import Runtime
from cue_notify.services import recipients as recipient_service

DELIVER_JOB = "deliver"


@dataclass(slots=True)
class Intent:
    recipient: Recipient
    template: Template
    category: Category
    channels: list[str]
    data: dict[str, Any] = field(default_factory=dict)
    event: Event | None = None
    rule: Rule | None = None
    broadcast_id: UUID | None = None
    send_at: datetime | None = None
    delay: timedelta = timedelta()
    cooldown: timedelta | None = None
    dedup_key: str | None = None
    personalize: bool = False
    """Ask the AI layer to reword even if the template has no ``ai_instructions``."""
    ai_context: dict[str, Any] | None = None
    """Extra, explicitly allowed data the AI may see (e.g. grouped digest items)."""


@dataclass(slots=True)
class Decision:
    status: MessageStatus
    reason: str | None = None
    send_at: datetime | None = None
    channels: list[str] = field(default_factory=list)
    locale: str | None = None
    variants: dict[str, dict[str, Any]] = field(default_factory=dict)
    existing: Message | None = None


def template_context(intent: Intent) -> dict[str, Any]:
    event = intent.event
    return {
        "data": intent.data,
        "recipient": recipient_service.profile(intent.recipient),
        "event": (
            {"name": event.name, "source": event.source, "occurred_at": event.occurred_at}
            if event
            else None
        ),
        "category": intent.category.key,
    }


async def _count_recent(session: AsyncSession, *conditions: Any, since: datetime) -> int:
    stmt = select(func.count(Message.id)).where(
        *conditions, Message.status.in_(COUNTED_STATUSES), Message.send_at > since
    )
    return int(await session.scalar(stmt) or 0)


async def decide(session: AsyncSession, runtime: Runtime, intent: Intent) -> Decision:
    """Evaluate policy for ``intent`` without writing anything."""
    recipient = intent.recipient
    category = intent.category
    prefs = recipient_service.preferences(recipient)
    defaults = runtime.settings.policy

    if intent.dedup_key:
        existing = await session.scalar(
            select(Message).where(Message.dedup_key == intent.dedup_key)
        )
        if existing is not None:
            return Decision(
                MessageStatus.SUPPRESSED, SuppressionReason.DUPLICATE, existing=existing
            )

    if category.allow_unsubscribe and category.key in prefs.unsubscribed_categories:
        return Decision(MessageStatus.SUPPRESSED, SuppressionReason.UNSUBSCRIBED)

    reachable = {
        address.channel
        for address in recipient.addresses
        if address.enabled
        and address.channel in runtime.channels
        and address.channel not in prefs.muted_channels
    }
    channels = policy.first_reachable(intent.channels, reachable)
    if not channels:
        return Decision(MessageStatus.SUPPRESSED, SuppressionReason.NO_REACHABLE_CHANNEL)

    send_at = intent.send_at or (utcnow() + intent.delay)
    if category.respect_quiet_hours:
        window = prefs.quiet_hours if prefs.quiet_hours is not None else defaults.quiet_hours
        tz = policy.zone(recipient.timezone, defaults.default_timezone)
        send_at = policy.defer_past_quiet_hours(send_at, tz, window)

    if intent.rule is not None and intent.cooldown:
        recent = await _count_recent(
            session,
            Message.recipient_id == recipient.id,
            Message.rule_id == intent.rule.id,
            since=send_at - intent.cooldown,
        )
        if recent:
            return Decision(MessageStatus.SUPPRESSED, SuppressionReason.COOLDOWN, send_at=send_at)

    for raw_cap in category.frequency_caps:
        cap = FrequencyCap.model_validate(raw_cap)
        recent = await _count_recent(
            session,
            Message.recipient_id == recipient.id,
            Message.category_key == category.key,
            since=send_at - cap.period,
        )
        if recent >= cap.limit:
            return Decision(
                MessageStatus.SUPPRESSED,
                f"{SuppressionReason.FREQUENCY_CAP}: {cap.limit} per {cap.period}",
                send_at=send_at,
            )

    template = intent.template
    locale = policy.pick_locale(
        template.locales, recipient.locale or defaults.default_locale, template.default_locale
    )
    if locale is None:
        return Decision(MessageStatus.FAILED, f"{SuppressionReason.TEMPLATE_ERROR}: no locales")
    try:
        localized = LocalizedTemplate.model_validate(template.locales[locale])
        rendered = localized.render_variants(template_context(intent))
    except (RenderError, ValueError) as exc:
        return Decision(
            MessageStatus.FAILED, f"{SuppressionReason.TEMPLATE_ERROR}: {exc}", locale=locale
        )
    variants = {
        name: content.model_dump(exclude_none=True)
        for name, content in rendered.items()
        if name == DEFAULT_VARIANT or name in channels
    }
    return Decision(
        MessageStatus.QUEUED, send_at=send_at, channels=channels, locale=locale, variants=variants
    )


async def compose(session: AsyncSession, runtime: Runtime, intent: Intent) -> tuple[Message, bool]:
    """Decide and persist. Returns ``(message, created)``; duplicates return the original.

    Must run inside a transaction. The recipient row is locked so that concurrent
    messages for the same person see each other when counting caps and cooldowns.
    """
    await session.execute(
        select(Recipient.id).where(Recipient.id == intent.recipient.id).with_for_update()
    )
    decision = await decide(session, runtime, intent)
    if decision.existing is not None:
        return decision.existing, False

    message = Message(
        recipient_id=intent.recipient.id,
        event_id=intent.event.id if intent.event else None,
        rule_id=intent.rule.id if intent.rule else None,
        broadcast_id=intent.broadcast_id,
        template_key=intent.template.key,
        template_version=intent.template.version,
        category_key=intent.category.key,
        channels=decision.channels or list(intent.channels),
        locale=decision.locale,
        content=decision.variants,
        personalize=bool(intent.template.ai_instructions) or intent.personalize,
        ai_context=intent.ai_context,
        status=decision.status,
        status_reason=decision.reason,
        dedup_key=intent.dedup_key,
        send_at=decision.send_at or utcnow(),
    )
    session.add(message)
    await session.flush()
    if decision.status is not MessageStatus.QUEUED:
        runtime.metrics.message_finished(message.status, None, message.category_key)
    else:
        await queue.enqueue(
            session,
            DELIVER_JOB,
            {"message_id": str(message.id)},
            run_at=message.send_at,
            key=f"{DELIVER_JOB}:{message.id}",
            max_attempts=runtime.settings.worker.max_attempts,
        )
    return message, True
