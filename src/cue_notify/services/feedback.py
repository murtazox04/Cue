from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.core.enums import EngagementType, FeedbackType, MessageStatus, PreferenceSource
from cue_notify.core.time import utcnow
from cue_notify.db.dml import insert_ignore
from cue_notify.db.models import (
    Address,
    FeedbackReport,
    Message,
    MessageDelivery,
    PreferenceChange,
    Recipient,
)
from cue_notify.runtime import Runtime
from cue_notify.services import engagement


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
    report_id: str | None = Field(
        default=None,
        max_length=255,
        description="The provider's id for this report. A report whose id was seen before "
        "is ignored.",
    )
    reason: str | None = Field(
        default=None, max_length=300, examples=["550 5.1.1 mailbox unavailable"]
    )
    occurred_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def identifies_something(self) -> Self:
        if not (self.address or self.message_id or self.provider_message_id):
            raise ValueError("give the address, the message_id or the provider_message_id")
        return self


@dataclass(slots=True)
class FeedbackResult:
    message_id: UUID | None = None
    recipients: list[str] = field(default_factory=list)
    disabled: int = 0
    unsubscribed: bool = False
    duplicate: bool = False


@dataclass(slots=True)
class Subject:
    message: Message | None = None
    deliveries: list[MessageDelivery] = field(default_factory=list)


async def locate_channel(
    session: AsyncSession, channels: Collection[str], provider_message_id: str
) -> str | None:
    if not channels:
        return None
    found = await session.scalar(
        select(MessageDelivery.channel)
        .where(
            MessageDelivery.channel.in_(channels),
            MessageDelivery.provider_message_id == provider_message_id,
        )
        .limit(1)
    )
    if found is not None:
        return found
    return await session.scalar(
        select(Message.channel)
        .where(Message.channel.in_(channels), Message.provider_message_id == provider_message_id)
        .limit(1)
    )


async def find_subject(session: AsyncSession, data: FeedbackIn) -> Subject:
    message_id = data.message_id
    if message_id is None and data.provider_message_id:
        message_id = await session.scalar(
            select(MessageDelivery.message_id)
            .where(
                MessageDelivery.channel == data.channel,
                MessageDelivery.provider_message_id == data.provider_message_id,
            )
            .limit(1)
        )
        if message_id is None:
            message_id = await session.scalar(
                select(Message.id).where(
                    Message.channel == data.channel,
                    Message.provider_message_id == data.provider_message_id,
                )
            )
    if message_id is None:
        return Subject()
    message = await session.get(Message, message_id, with_for_update=True)
    if message is None or message.channel not in (None, data.channel):
        return Subject()
    stmt = select(MessageDelivery).where(
        MessageDelivery.message_id == message.id, MessageDelivery.channel == data.channel
    )
    if data.provider_message_id and data.message_id is None:
        stmt = stmt.where(MessageDelivery.provider_message_id == data.provider_message_id)
    if data.address is not None:
        stmt = stmt.where(MessageDelivery.address == data.address)
    deliveries = list(await session.scalars(stmt.order_by(MessageDelivery.created_at)))
    return Subject(message, deliveries)


async def find_addresses(
    session: AsyncSession, data: FeedbackIn, subject: Subject
) -> list[Address]:
    stmt = select(Address).where(Address.channel == data.channel)
    message = subject.message
    if data.address is not None:
        stmt = stmt.where(Address.value == data.address)
    elif subject.deliveries:
        stmt = stmt.where(Address.value.in_({d.address for d in subject.deliveries}))
    elif message is None:
        return []
    if message is not None:
        stmt = stmt.where(Address.recipient_id == message.recipient_id)
    return list(await session.scalars(stmt.order_by(Address.created_at).with_for_update()))


async def apply(
    session: AsyncSession,
    runtime: Runtime,
    data: FeedbackIn,
    provenance: dict[str, Any],
    *,
    require_message: bool = False,
) -> FeedbackResult:
    if data.report_id and not await insert_ignore(
        session,
        FeedbackReport,
        {"report_id": data.report_id, "received_at": utcnow()},
        ["report_id"],
    ):
        return FeedbackResult(duplicate=True)
    subject = await find_subject(session, data)
    message = subject.message
    if message is None and require_message:
        return FeedbackResult()
    addresses = await find_addresses(session, data, subject)
    pinpointed = data.address is not None or (
        len(addresses) <= 1 and len({d.address for d in subject.deliveries}) <= 1
    )
    result = FeedbackResult(message_id=message.id if message else None)
    reason = (data.reason or data.type.value)[:200]

    match data.type:
        case FeedbackType.DELIVERED:
            for address in addresses if pinpointed else []:
                address.soft_bounces = 0
            if message is not None and message.status is MessageStatus.SENT:
                engagement.record(
                    message,
                    engagement.EngagementIn(
                        type=EngagementType.DELIVERED, occurred_at=data.occurred_at
                    ),
                )
        case FeedbackType.BOUNCE:
            for address in addresses if pinpointed else []:
                result.disabled += disable(address, f"bounced: {reason}")
            if pinpointed:
                for delivery in subject.deliveries:
                    delivery.bounced = True
            if message is not None and message.status is MessageStatus.SENT:
                await session.flush()
                if await every_delivery_bounced(session, message):
                    message.status = MessageStatus.FAILED
                    message.status_reason = f"bounced: {reason}"[:500]
        case FeedbackType.SOFT_BOUNCE:
            if not pinpointed:
                addresses = []
            counted = {d.address for d in subject.deliveries if not d.soft_bounced}
            for delivery in subject.deliveries if pinpointed else []:
                delivery.soft_bounced = True
            limit = runtime.settings.delivery.soft_bounce_limit
            for address in addresses:
                if subject.deliveries and address.value not in counted:
                    continue
                address.soft_bounces += 1
                if address.soft_bounces >= limit:
                    detail = f"{address.soft_bounces} soft bounces in a row: {reason}"
                    result.disabled += disable(address, detail)
        case FeedbackType.COMPLAINT:
            result.unsubscribed = await complain(session, data, message, addresses, provenance)
    result.recipients = await external_ids(
        session,
        {a.recipient_id for a in addresses} | ({message.recipient_id} if message else set()),
    )
    return result


async def every_delivery_bounced(session: AsyncSession, message: Message) -> bool:
    flags = list(
        await session.scalars(
            select(MessageDelivery.bounced).where(MessageDelivery.message_id == message.id)
        )
    )
    return all(flags)


async def complain(
    session: AsyncSession,
    data: FeedbackIn,
    message: Message | None,
    addresses: list[Address],
    provenance: dict[str, Any],
) -> bool:
    now = utcnow()
    fresh = [a for a in addresses if a.complained_at is None]
    for address in fresh:
        address.complained_at = now
    ledger = {
        **provenance,
        "method": "complaint",
        "channel": data.channel,
        **({"address": data.address} if data.address else {}),
        **({"message_id": str(message.id)} if message else {}),
    }
    owners: dict[UUID, list[str]] = {}
    for address in fresh:
        owners.setdefault(address.recipient_id, []).append(address.value)
    for recipient_id, values in sorted(owners.items()):
        session.add(
            PreferenceChange(
                recipient_id=recipient_id,
                source=PreferenceSource.COMPLAINT,
                changes={"complained": {data.channel: sorted(values)}},
                provenance=ledger,
            )
        )
    return bool(fresh)


def disable(address: Address, reason: str) -> int:
    if not address.enabled:
        return 0
    address.enabled = False
    address.disabled_reason = reason[:255]
    return 1


async def external_ids(session: AsyncSession, ids: set[UUID]) -> list[str]:
    if not ids:
        return []
    return sorted(await session.scalars(select(Recipient.external_id).where(Recipient.id.in_(ids))))
