"""Delivery: send a queued message through its planned channels, with fallback.

Order of operations for one ``deliver`` job:

1. Re-check the message is still queued and the recipient still wants it.
2. Optionally personalise the copy with AI (stored, so retries reuse it).
3. Try each planned channel in order. Within a channel the message goes to every
   enabled address (all of a user's devices); one success is enough.
   * dead addresses are disabled;
   * transient errors retry the job with backoff before falling back;
   * permanent errors fall through to the next channel.
4. Record the outcome.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select, update

from cue_notify import queue
from cue_notify.ai import GenerationError, PersonalizationRequest
from cue_notify.channels.base import Delivery, DeliveryError, InvalidAddressError, Receipt
from cue_notify.core.content import DEFAULT_VARIANT, Content
from cue_notify.core.enums import MessageStatus, SuppressionReason
from cue_notify.core.time import utcnow
from cue_notify.db.dml import upsert_increment
from cue_notify.db.models import AiUsage, Category, Message, Recipient, Template
from cue_notify.runtime import Runtime
from cue_notify.services import recipients as recipient_service

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChannelAttempt:
    channel: str
    receipt: Receipt | None = None
    invalid: list[tuple[str, str]] = field(default_factory=list)
    retryable: DeliveryError | None = None
    error: str | None = None


async def _send_to_addresses(
    runtime: Runtime, message: Message, channel_name: str, addresses: list[str], content: Content
) -> ChannelAttempt:
    channel = runtime.channels[channel_name]
    metadata = {
        "cue_message_id": str(message.id),
        "cue_tracking_token": message.tracking_token,
    }
    deliveries = [
        Delivery(message.id, address, content, locale=message.locale, metadata=metadata)
        for address in addresses
    ]
    results = await asyncio.gather(
        *(channel.send(delivery) for delivery in deliveries), return_exceptions=True
    )
    attempt = ChannelAttempt(channel_name)
    for address, result in zip(addresses, results, strict=True):
        if isinstance(result, Receipt):
            attempt.receipt = attempt.receipt or result
        elif isinstance(result, InvalidAddressError):
            attempt.invalid.append((address, str(result)))
        elif isinstance(result, DeliveryError):
            if result.retryable:
                attempt.retryable = result
            else:
                attempt.error = str(result)
        else:
            logger.error(
                "channel %s raised unexpectedly",
                channel_name,
                exc_info=result,
                extra={"message_id": str(message.id)},
            )
            attempt.retryable = DeliveryError(f"{type(result).__name__}: {result}", retryable=True)
    if (
        attempt.receipt is None
        and attempt.error is None
        and attempt.invalid
        and not attempt.retryable
    ):
        attempt.error = "; ".join(reason for _, reason in attempt.invalid)
    return attempt


async def _within_budget(runtime: Runtime) -> bool:
    budget = runtime.settings.ai.daily_token_budget
    if budget is None:
        return True
    async with runtime.session() as session:
        usage = await session.get(AiUsage, utcnow().date())
    return usage is None or usage.tokens < budget


async def _personalize(runtime: Runtime, message: Message, recipient: Recipient) -> None:
    """Rewrite the default variant with AI; any failure keeps the template text."""
    generator = runtime.generator
    if generator is None or not message.personalize or message.generated_by != "template":
        return
    if not await _within_budget(runtime):
        logger.info(
            "AI budget exhausted; using template copy", extra={"message_id": str(message.id)}
        )
        return
    async with runtime.session() as session:
        template = await session.scalar(
            select(Template).where(Template.key == message.template_key)
        )
    if template is None or not template.ai_instructions:
        return
    baseline = Content.model_validate(message.content[DEFAULT_VARIANT])
    context = {
        key: recipient.attributes[key]
        for key in template.ai_attributes
        if key in recipient.attributes
    }
    request = PersonalizationRequest(
        baseline=baseline,
        locale=message.locale or runtime.settings.policy.default_locale,
        channel=message.channels[0] if message.channels else "push",
        instructions=template.ai_instructions,
        context=context,
    )
    try:
        result = await generator.personalize(request)
    except GenerationError as exc:
        logger.warning("AI personalisation failed: %s", exc, extra={"message_id": str(message.id)})
        return
    except Exception:  # AI is an enhancement; it must never block delivery
        logger.exception("AI personalisation raised", extra={"message_id": str(message.id)})
        return
    content = dict(message.content)
    content[DEFAULT_VARIANT] = result.apply(baseline).model_dump(exclude_none=True)
    async with runtime.transaction() as session:
        await upsert_increment(
            session, AiUsage, {"day": utcnow().date()}, {"tokens": result.tokens, "requests": 1}
        )
        stored = await session.get(Message, message.id)
        if stored is not None:
            stored.content = content
            stored.generated_by = f"ai:{result.model}"[:128]
            stored.ai_tokens = result.tokens
    message.content = content
    message.generated_by = f"ai:{result.model}"


def _content_for(message: Message, channel: str) -> Content:
    variant = message.content.get(channel) or message.content[DEFAULT_VARIANT]
    return Content.model_validate(variant)


async def deliver(ctx: queue.JobContext) -> None:
    """Job handler for ``deliver``."""
    runtime = ctx.runtime
    message_id = UUID(ctx.payload["message_id"])

    async with runtime.transaction() as session:
        # Claim the message. Once it is "sending" it can no longer be cancelled, so a
        # cancel can never race with a send and leave a delivered message "cancelled".
        # A message left "sending" by a crashed attempt is picked up again.
        message = await session.get(Message, message_id, with_for_update=True)
        if message is None or message.status not in (MessageStatus.QUEUED, MessageStatus.SENDING):
            return  # cancelled, already finished by a previous attempt, or deleted
        recipient = await session.get(Recipient, message.recipient_id)
        category = await session.get(Category, message.category_key)
        if recipient is None:
            return
        prefs = recipient_service.preferences(recipient)
        if (
            category is not None
            and category.allow_unsubscribe
            and category.key in prefs.unsubscribed_categories
        ):
            message.status = MessageStatus.SUPPRESSED
            message.status_reason = SuppressionReason.UNSUBSCRIBED
            return
        message.status = MessageStatus.SENDING
        message.attempts += 1
        targets = {
            channel: [
                address.value
                for address in recipient.addresses
                if address.channel == channel and address.enabled
            ]
            for channel in message.channels
            if channel in runtime.channels and channel not in prefs.muted_channels
        }

    await _personalize(runtime, message, recipient)

    errors: list[str] = []
    invalid: list[tuple[str, str, str]] = []
    delivered: ChannelAttempt | None = None
    for channel, addresses in targets.items():
        if not addresses:
            continue
        attempt = await _send_to_addresses(
            runtime, message, channel, addresses, _content_for(message, channel)
        )
        invalid += [(channel, address, reason) for address, reason in attempt.invalid]
        if attempt.receipt is not None:
            delivered = attempt
            break
        if attempt.retryable is not None and not ctx.is_last_attempt:
            # Waiting for a retry: hand the message back so it can still be cancelled.
            await _disable_addresses(runtime, message.recipient_id, invalid)
            await _release(runtime, message_id)
            delay = attempt.retryable.retry_after
            if delay is not None:
                raise queue.RetryLater(delay, f"{channel}: {attempt.retryable}")
            raise attempt.retryable
        errors.append(f"{channel}: {attempt.error or attempt.retryable}")

    await _disable_addresses(runtime, message.recipient_id, invalid)
    async with runtime.transaction() as session:
        stored = await session.get(Message, message_id, with_for_update=True)
        if stored is None or stored.status is not MessageStatus.SENDING:
            return
        if delivered is not None:
            stored.status = MessageStatus.SENT
            stored.channel = delivered.channel
            stored.sent_at = utcnow()
            stored.provider_message_id = (
                delivered.receipt.provider_message_id if delivered.receipt else None
            )
            stored.status_reason = None
        else:
            reason = "; ".join(errors) or SuppressionReason.NO_REACHABLE_CHANNEL
            stored.status = MessageStatus.FAILED
            stored.status_reason = reason[:500]
        runtime.metrics.message_finished(stored.status, stored.channel, stored.category_key)
    logger.info(
        "message %s",
        "sent" if delivered else "failed",
        extra={"message_id": str(message_id), "channel": delivered.channel if delivered else None},
    )


async def _release(runtime: Runtime, message_id: UUID) -> None:
    async with runtime.transaction() as session:
        await session.execute(
            update(Message)
            .where(Message.id == message_id, Message.status == MessageStatus.SENDING)
            .values(status=MessageStatus.QUEUED)
        )


async def deliver_dead(ctx: queue.JobContext, error: str) -> None:
    """Retries exhausted: record the failure so the message stops counting as in flight."""
    async with ctx.runtime.transaction() as session:
        await session.execute(
            update(Message)
            .where(
                Message.id == UUID(ctx.payload["message_id"]),
                Message.status.in_((MessageStatus.QUEUED, MessageStatus.SENDING)),
            )
            .values(status=MessageStatus.FAILED, status_reason=f"gave up: {error}"[:500])
        )


async def _disable_addresses(
    runtime: Runtime, recipient_id: UUID, invalid: list[tuple[str, str, str]]
) -> None:
    if not invalid:
        return
    async with runtime.transaction() as session:
        recipient = await session.get(Recipient, recipient_id)
        if recipient is None:
            return
        for address in recipient.addresses:
            for channel, value, reason in invalid:
                if address.channel == channel and address.value == value:
                    address.enabled = False
                    address.disabled_reason = reason[:255]
    invalid.clear()
