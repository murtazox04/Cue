"""Delivery feedback: what providers report after Cue handed a message over.

* **delivered** marks the message delivered and clears the address's soft bounces;
* **bounce** disables the address: it will never work (unknown mailbox, dead number);
* **soft_bounce** counts against the address; ``delivery.soft_bounce_limit`` in a row,
  with no delivery between them, disable it;
* **complaint** (marked as spam) unsubscribes the person from every optional category,
  recorded in the consent ledger with source ``complaint``. The address stays usable
  for messages people cannot opt out of, such as receipts and security alerts.

A bounce also turns a ``sent`` message ``failed``. Disabled addresses come back only
through ``POST /v1/recipients/{id}/addresses``, so a later profile update cannot revive
an address the provider said is dead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.core.enums import EngagementType, FeedbackType, MessageStatus, PreferenceSource
from cue_notify.db.models import Address, Category, Message, Recipient
from cue_notify.runtime import Runtime
from cue_notify.services import engagement, preferences
from cue_notify.services import recipients as recipient_service


class FeedbackIn(BaseModel):
    type: FeedbackType
    channel: str = Field(min_length=1, max_length=64, examples=["email"])
    address: str | None = Field(
        default=None, max_length=2048, description="The address it concerns.", examples=["a@b.co"]
    )
    message_id: UUID | None = Field(default=None, description="Cue's message id, if known.")
    provider_message_id: str | None = Field(
        default=None, max_length=255, description="The provider's id, as returned on sending."
    )
    reason: str | None = Field(
        default=None, max_length=300, examples=["550 5.1.1 mailbox unavailable"]
    )
    occurred_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _identifies_something(self) -> Self:
        if not (self.address or self.message_id or self.provider_message_id):
            raise ValueError("give the address, the message_id or the provider_message_id")
        return self


@dataclass(slots=True)
class FeedbackResult:
    message_id: UUID | None = None
    recipients: list[str] = field(default_factory=list)
    disabled: int = 0
    """Addresses disabled by this report."""
    unsubscribed: bool = False


async def _message(session: AsyncSession, data: FeedbackIn) -> Message | None:
    if data.message_id is not None:
        message = await session.get(Message, data.message_id, with_for_update=True)
    elif data.provider_message_id:
        message = await session.scalar(
            select(Message)
            .where(
                Message.channel == data.channel,
                Message.provider_message_id == data.provider_message_id,
            )
            .with_for_update()
        )
    else:
        return None
    if message is None or message.channel not in (None, data.channel):
        return None  # another channel's message: don't act on the wrong recipient
    return message


async def _addresses(
    session: AsyncSession, data: FeedbackIn, message: Message | None
) -> list[Address]:
    stmt = select(Address).where(Address.channel == data.channel)
    if data.address is not None:
        stmt = stmt.where(Address.value == data.address)
        if message is not None:
            stmt = stmt.where(Address.recipient_id == message.recipient_id)
    elif message is not None:
        stmt = stmt.where(Address.recipient_id == message.recipient_id)
    else:
        return []
    found = list(await session.scalars(stmt.with_for_update()))
    if data.address is None and len(found) > 1:
        return []  # the message went to several addresses; which one bounced is unknown
    return found


async def apply(
    session: AsyncSession, runtime: Runtime, data: FeedbackIn, provenance: dict[str, Any]
) -> FeedbackResult:
    """Apply one report. Repeating a report is harmless, except that a repeated soft
    bounce counts again."""
    message = await _message(session, data)
    addresses = await _addresses(session, data, message)
    result = FeedbackResult(message_id=message.id if message else None)
    reason = (data.reason or data.type.value)[:200]

    match data.type:
        case FeedbackType.DELIVERED:
            for address in addresses:
                address.soft_bounces = 0
            if message is not None and message.status is MessageStatus.SENT:
                engagement.record(
                    message,
                    engagement.EngagementIn(
                        type=EngagementType.DELIVERED, occurred_at=data.occurred_at
                    ),
                )
        case FeedbackType.BOUNCE:
            for address in addresses:
                result.disabled += _disable(address, f"bounced: {reason}")
            if message is not None and message.status is MessageStatus.SENT:
                message.status = MessageStatus.FAILED
                message.status_reason = f"bounced: {reason}"[:500]
        case FeedbackType.SOFT_BOUNCE:
            limit = runtime.settings.delivery.soft_bounce_limit
            for address in addresses:
                address.soft_bounces += 1
                if address.soft_bounces >= limit:
                    detail = f"{address.soft_bounces} soft bounces in a row: {reason}"
                    result.disabled += _disable(address, detail)
        case FeedbackType.COMPLAINT:
            owners = {a.recipient_id for a in addresses}
            if message is not None:
                owners.add(message.recipient_id)
            optional = set(
                await session.scalars(select(Category.key).where(Category.allow_unsubscribe))
            )
            ledger = {
                **provenance,
                "method": "complaint",
                "channel": data.channel,
                **({"address": data.address} if data.address else {}),
                **({"message_id": str(message.id)} if message else {}),
            }
            for recipient_id in sorted(owners):
                recipient = await session.get(Recipient, recipient_id, with_for_update=True)
                if recipient is None:
                    continue
                prefs = recipient_service.preferences(recipient)
                updated = prefs.model_copy(
                    update={"unsubscribed_categories": prefs.unsubscribed_categories | optional}
                )
                changed = await preferences.apply(
                    session, recipient, updated, PreferenceSource.COMPLAINT, ledger
                )
                result.unsubscribed = result.unsubscribed or changed is not None
    result.recipients = await _external_ids(
        session,
        {a.recipient_id for a in addresses} | ({message.recipient_id} if message else set()),
    )
    return result


def _disable(address: Address, reason: str) -> int:
    if not address.enabled:
        return 0
    address.enabled = False
    address.disabled_reason = reason[:255]
    return 1


async def _external_ids(session: AsyncSession, ids: set[UUID]) -> list[str]:
    if not ids:
        return []
    return sorted(await session.scalars(select(Recipient.external_id).where(Recipient.id.in_(ids))))
