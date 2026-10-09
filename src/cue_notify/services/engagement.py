"""Delivery receipts and engagement (opened, clicked, converted)."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, Field

from cue_notify.core.enums import EngagementType, MessageStatus
from cue_notify.core.time import ensure_utc, utcnow
from cue_notify.db.models import Message
from cue_notify.services.errors import InvalidStateError


class EngagementIn(BaseModel):
    type: EngagementType
    occurred_at: AwareDatetime | None = None
    value: float | None = Field(default=None, description="Conversion value, if any.")


def record(message: Message, data: EngagementIn) -> Message:
    """Apply an engagement signal. Idempotent: the first report of each kind wins.

    Later stages imply earlier ones (a click implies the message was delivered and opened).
    Client-supplied times are clamped between the send time and now, because the tracking
    endpoint is reachable by anyone holding the message's token.
    """
    if message.status not in (MessageStatus.SENT, MessageStatus.DELIVERED):
        raise InvalidStateError(f"message is {message.status}; only sent messages take engagement")
    now = utcnow()
    earliest = message.sent_at or message.created_at
    at = now
    if data.occurred_at is not None:
        try:
            at = min(max(ensure_utc(data.occurred_at), earliest), now)
        except OverflowError:  # e.g. year 1 with a positive offset
            at = now
    stages = list(EngagementType)
    reached = stages[: stages.index(data.type) + 1]
    if EngagementType.DELIVERED in reached:
        message.delivered_at = message.delivered_at or at
        message.status = MessageStatus.DELIVERED
    if EngagementType.OPENED in reached:
        message.opened_at = message.opened_at or at
    if EngagementType.CLICKED in reached:
        message.clicked_at = message.clicked_at or at
    if data.type is EngagementType.CONVERTED and message.converted_at is None:
        message.converted_at = at
        message.conversion_value = data.value
    return message
