"""Delivery receipts and engagement (opened, clicked, converted)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from cue_notify.core.enums import EngagementType, MessageStatus
from cue_notify.core.time import ensure_utc, utcnow
from cue_notify.db.models import Message
from cue_notify.services.errors import InvalidStateError


class EngagementIn(BaseModel):
    type: EngagementType
    occurred_at: datetime | None = None
    value: float | None = Field(default=None, description="Conversion value, if any.")


def record(message: Message, data: EngagementIn) -> Message:
    """Apply an engagement signal. Idempotent: the first timestamp of each kind wins.

    Later stages imply earlier ones (a click implies the message was delivered and opened).
    """
    if message.status not in (MessageStatus.SENT, MessageStatus.DELIVERED):
        raise InvalidStateError(f"message is {message.status}; only sent messages take engagement")
    at = ensure_utc(data.occurred_at) if data.occurred_at else utcnow()
    stages = list(EngagementType)
    reached = stages[: stages.index(data.type) + 1]
    if EngagementType.DELIVERED in reached:
        message.delivered_at = message.delivered_at or at
        message.status = MessageStatus.DELIVERED
    if EngagementType.OPENED in reached:
        message.opened_at = message.opened_at or at
    if EngagementType.CLICKED in reached:
        message.clicked_at = message.clicked_at or at
    if data.type is EngagementType.CONVERTED:
        message.converted_at = message.converted_at or at
        if data.value is not None:
            message.conversion_value = data.value
    return message
