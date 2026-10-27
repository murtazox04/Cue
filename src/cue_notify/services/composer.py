"""Turns an *intent to notify* into a queued (or suppressed) message.

Every path that sends something — event rules, the direct send API and broadcasts —
goes through :func:`compose`, so policy (preferences, reachability, cooldowns,
frequency caps, quiet hours) is applied uniformly and every decision is recorded.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify import queue
from cue_notify.core import compliance as legal
from cue_notify.core import logic, policy
from cue_notify.core.content import DEFAULT_VARIANT, LocalizedTemplate
from cue_notify.core.enums import COUNTED_STATUSES, Importance, MessageStatus, SuppressionReason
from cue_notify.core.policy import Fatigue, FrequencyCap
from cue_notify.core.templating import RenderError
from cue_notify.core.time import utcnow
from cue_notify.db.models import (
    Address,
    Agent,
    Category,
    Event,
    Message,
    Recipient,
    Rule,
    Template,
)
from cue_notify.runtime import Runtime
from cue_notify.services import preferences
from cue_notify.services import recipients as recipient_service
from cue_notify.settings import Settings

DELIVER_JOB = "deliver"
REVIEW_JOB = "request_review"


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
    importance: Importance = Importance.NORMAL
    expires_at: datetime | None = None
    agent: Agent | None = None
    """The automated sender, whose budget and approval policy apply on top."""


@dataclass(slots=True)
class Decision:
    status: MessageStatus
    reason: str | None = None
    send_at: datetime | None = None
    channels: list[str] = field(default_factory=list)
    locale: str | None = None
    variants: dict[str, dict[str, Any]] = field(default_factory=dict)
    existing: Message | None = None


def expiry(rule: Rule) -> datetime | None:
    """When a message from ``rule`` composed now stops being worth delivering."""
    if rule.expires_after_seconds is None:
        return None
    return utcnow() + timedelta(seconds=rule.expires_after_seconds)


def effective_importance(intent: Intent) -> Importance:
    """The importance policy will act on.

    An agent cannot go above its ``max_importance``, and ``critical`` is honoured only in
    categories people cannot unsubscribe from, so a marketing sender cannot use it to
    break through quiet hours and caps.
    """
    return policy.honoured_importance(
        intent.importance,
        unsubscribable=intent.category.allow_unsubscribe,
        ceiling=intent.agent.max_importance if intent.agent is not None else None,
    )


def agent_refusal(agent: Agent | None, category_key: str) -> SuppressionReason | None:
    if agent is None:
        return SuppressionReason.AGENT_DELETED
    if not agent.enabled or (agent.categories is not None and category_key not in agent.categories):
        return SuppressionReason.AGENT_NOT_ALLOWED
    return None


def _needs_approval(intent: Intent, importance: Importance) -> bool:
    agent = intent.agent
    if agent is None or agent.approval is None or agent.approval is False:
        return False
    if agent.approval is True:
        return True
    context = {
        "message": {
            "category": intent.category.key,
            "importance": importance.value,
            "channels": list(intent.channels),
            "template": intent.template.key,
        },
        "recipient": recipient_service.profile(intent.recipient),
        "data": intent.data,
    }
    return logic.matches(agent.approval, context)


def template_context(intent: Intent, settings: Settings) -> dict[str, Any]:
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
        "importance": effective_importance(intent).value,
        "links": preferences.template_links(settings, intent.recipient, intent.category),
    }


def breaks_through(importance: Importance, category: Category) -> bool:
    return policy.breaks_through(importance, unsubscribable=category.allow_unsubscribe)


def texting_window(
    recipient: Recipient, category: Category, channels: Sequence[str], settings: Settings
) -> legal.Window | None:
    compliance = settings.compliance
    if not (
        compliance.us_texting_rules
        and category.marketing
        and any(channel in compliance.sms_channels for channel in channels)
    ):
        return None
    phone = next(
        (a.value for a in recipient.addresses if a.channel in compliance.sms_channels),
        None,
    )
    return legal.window_for(recipient.region, phone, unknown_state=compliance.unknown_state)


def allowed_send_time(
    when: datetime,
    recipient: Recipient,
    category: Category,
    importance: Importance,
    channels: Sequence[str],
    settings: Settings,
) -> datetime:
    defaults = settings.policy
    tz = policy.zone(recipient.timezone, defaults.default_timezone)
    quiet = None
    if category.respect_quiet_hours and not breaks_through(importance, category):
        prefs = recipient_service.preferences(recipient)
        quiet = prefs.quiet_hours if prefs.quiet_hours is not None else defaults.quiet_hours
    window = texting_window(recipient, category, channels, settings)
    return policy.send_time(when, tz, quiet, window)


def receives(address: Address, category: Category | None) -> bool:
    if not address.enabled:
        return False
    return address.complained_at is None or (
        category is not None and not category.allow_unsubscribe
    )


def usable_channels(
    message: Message, recipient: Recipient, category: Category, runtime: Runtime
) -> list[str]:
    muted = recipient_service.preferences(recipient).muted_channels
    return [
        channel
        for channel in message.channels
        if channel in runtime.channels
        and channel not in muted
        and any(a.channel == channel and receives(a, category) for a in recipient.addresses)
    ]


def message_send_time(
    when: datetime, message: Message, recipient: Recipient, category: Category, runtime: Runtime
) -> datetime:
    channels = usable_channels(message, recipient, category, runtime)
    return allowed_send_time(
        when, recipient, category, message.importance, channels, runtime.settings
    )


async def rolling_count(
    session: AsyncSession,
    *conditions: Any,
    at: datetime,
    period: timedelta,
    statuses: tuple[MessageStatus, ...] = COUNTED_STATUSES,
) -> int:
    stmt = select(Message.send_at).where(
        *conditions,
        Message.status.in_(statuses),
        Message.send_at > at - period,
        Message.send_at < at + period,
    )
    return policy.rolling_peak((await session.scalars(stmt)).all(), at, period)


async def _fatigued(
    session: AsyncSession,
    recipient: Recipient,
    category: Category,
    fatigue: Fatigue,
    send_at: datetime,
) -> bool:
    """Whether the person ignored the last ``fatigue.after`` messages of this category and
    the latest of them is still within the pause."""
    recent = (
        await session.execute(
            select(Message.send_at, Message.opened_at, Message.clicked_at, Message.converted_at)
            .where(
                Message.recipient_id == recipient.id,
                Message.category_key == category.key,
                Message.status.in_((MessageStatus.SENT, MessageStatus.DELIVERED)),
            )
            .order_by(Message.send_at.desc())
            .limit(fatigue.after)
        )
    ).all()
    if len(recent) < fatigue.after:
        return False
    if any(opened or clicked or converted for _, opened, clicked, converted in recent):
        return False
    return send_at - recent[0].send_at < fatigue.pause


async def _agent_budget_exceeded(
    session: AsyncSession, agent: Agent, recipient: Recipient, send_at: datetime
) -> str | None:
    """The reason to hold back, if this agent already used its budget for this person.

    Messages waiting for approval count, so an agent cannot flood the review queue.
    """
    for raw_cap in agent.frequency_caps:
        cap = FrequencyCap.model_validate(raw_cap)
        recent = await rolling_count(
            session,
            Message.recipient_id == recipient.id,
            Message.agent_key == agent.key,
            at=send_at,
            period=cap.period,
            statuses=(*COUNTED_STATUSES, MessageStatus.PENDING_APPROVAL),
        )
        if recent >= cap.limit:
            return policy.limit_reason(SuppressionReason.AGENT_BUDGET, cap)
    return None


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
    consent_until = prefs.consent_expires.get(category.key)
    if category.allow_unsubscribe and consent_until is not None and consent_until <= utcnow():
        return Decision(MessageStatus.SUPPRESSED, SuppressionReason.CONSENT_EXPIRED)

    reachable = {
        address.channel
        for address in recipient.addresses
        if receives(address, category)
        and address.channel in runtime.channels
        and address.channel not in prefs.muted_channels
    }
    channels = policy.first_reachable(intent.channels, reachable)
    if not channels:
        return Decision(MessageStatus.SUPPRESSED, SuppressionReason.NO_REACHABLE_CHANNEL)

    importance = effective_importance(intent)
    now = utcnow()
    # A time in the past means "now": counting caps from a backdated time would let a
    # sender spread messages over windows that have already closed.
    send_at = policy.earliest_send(now, intent.send_at, intent.delay)
    send_at = allowed_send_time(
        send_at, recipient, category, importance, channels, runtime.settings
    )

    window = texting_window(recipient, category, channels, runtime.settings)
    if window is not None and window.daily_cap is not None:
        marketing = select(Category.key).where(Category.marketing.is_(True))
        recent = await rolling_count(
            session,
            Message.recipient_id == recipient.id,
            Message.category_key.in_(marketing),
            at=send_at,
            period=timedelta(days=1),
        )
        if recent >= window.daily_cap:
            return Decision(
                MessageStatus.SUPPRESSED,
                f"{SuppressionReason.COMPLIANCE}: {window.daily_cap} marketing "
                f"texts per 24 hours ({window.source})",
                send_at=send_at,
            )

    if intent.rule is not None and intent.cooldown:
        recent = await rolling_count(
            session,
            Message.recipient_id == recipient.id,
            Message.rule_id == intent.rule.id,
            at=send_at,
            period=intent.cooldown,
        )
        if recent:
            return Decision(MessageStatus.SUPPRESSED, SuppressionReason.COOLDOWN, send_at=send_at)

    if policy.expired(send_at, intent.expires_at):
        return Decision(MessageStatus.SUPPRESSED, SuppressionReason.EXPIRED, send_at=send_at)

    for raw_cap in [] if breaks_through(importance, category) else category.frequency_caps:
        cap = FrequencyCap.model_validate(raw_cap)
        recent = await rolling_count(
            session,
            Message.recipient_id == recipient.id,
            Message.category_key == category.key,
            at=send_at,
            period=cap.period,
        )
        if recent >= cap.limit:
            return Decision(
                MessageStatus.SUPPRESSED,
                policy.limit_reason(SuppressionReason.FREQUENCY_CAP, cap),
                send_at=send_at,
            )

    if category.fatigue is not None and importance.rank <= Importance.NORMAL.rank:
        fatigue = Fatigue.model_validate(category.fatigue)
        if await _fatigued(session, recipient, category, fatigue, send_at):
            return Decision(MessageStatus.SUPPRESSED, SuppressionReason.FATIGUE, send_at=send_at)

    if intent.agent is not None:
        exceeded = await _agent_budget_exceeded(session, intent.agent, recipient, send_at)
        if exceeded is not None:
            return Decision(MessageStatus.SUPPRESSED, exceeded, send_at=send_at)

    template = intent.template
    locale = policy.pick_locale(
        template.locales, recipient.locale or defaults.default_locale, template.default_locale
    )
    if locale is None:
        return Decision(MessageStatus.FAILED, f"{SuppressionReason.TEMPLATE_ERROR}: no locales")
    try:
        localized = LocalizedTemplate.model_validate(template.locales[locale])
        rendered = localized.render_variants(template_context(intent, runtime.settings))
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
        MessageStatus.PENDING_APPROVAL
        if _needs_approval(intent, importance)
        else MessageStatus.QUEUED,
        send_at=send_at,
        channels=channels,
        locale=locale,
        variants=variants,
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

    expires_at = intent.expires_at
    if decision.status is MessageStatus.PENDING_APPROVAL and intent.agent is not None:
        deadline = utcnow() + timedelta(seconds=intent.agent.approval_ttl_seconds)
        expires_at = deadline if expires_at is None else min(expires_at, deadline)
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
        importance=effective_importance(intent),
        send_at=decision.send_at or utcnow(),
        expires_at=expires_at,
        agent_key=intent.agent.key if intent.agent else None,
    )
    session.add(message)
    await session.flush()
    if decision.status is MessageStatus.QUEUED:
        await enqueue_delivery(session, runtime, message)
    elif (
        decision.status is MessageStatus.PENDING_APPROVAL
        and intent.agent
        and intent.agent.reviewers
    ):
        await queue.enqueue(
            session,
            REVIEW_JOB,
            {"message_id": str(message.id)},
            key=f"{REVIEW_JOB}:{message.id}",
            max_attempts=runtime.settings.worker.max_attempts,
        )
    elif decision.status is not MessageStatus.PENDING_APPROVAL:  # held until reviewed
        runtime.metrics.message_finished(message.status, None, message.category_key)
    return message, True


async def enqueue_delivery(session: AsyncSession, runtime: Runtime, message: Message) -> None:
    await queue.enqueue(
        session,
        DELIVER_JOB,
        {"message_id": str(message.id)},
        run_at=message.send_at,
        key=f"{DELIVER_JOB}:{message.id}",
        max_attempts=runtime.settings.worker.max_attempts,
    )
