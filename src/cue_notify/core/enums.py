from __future__ import annotations

from enum import StrEnum


class MessageStatus(StrEnum):
    """Delivery lifecycle of a message. Engagement (opened, clicked…) is tracked separately."""

    QUEUED = "queued"
    """Accepted and waiting for its send time."""
    SENDING = "sending"
    """Claimed by a worker and being handed to a provider; can no longer be cancelled."""
    SENT = "sent"
    """Handed to a provider successfully."""
    DELIVERED = "delivered"
    """Provider or client confirmed delivery to the device/inbox."""
    FAILED = "failed"
    """Every planned channel failed permanently or retries were exhausted."""
    SUPPRESSED = "suppressed"
    """Not sent because a policy decided against it; see ``status_reason``."""
    CANCELLED = "cancelled"
    """Withdrawn before sending (e.g. its broadcast was cancelled)."""

    @property
    def is_final(self) -> bool:
        return self not in (MessageStatus.QUEUED, MessageStatus.SENDING)


#: Statuses that count towards frequency caps and cooldowns.
COUNTED_STATUSES = (
    MessageStatus.QUEUED,
    MessageStatus.SENDING,
    MessageStatus.SENT,
    MessageStatus.DELIVERED,
)


class EngagementType(StrEnum):
    DELIVERED = "delivered"
    OPENED = "opened"
    CLICKED = "clicked"
    CONVERTED = "converted"


class EventStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


class BroadcastStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class SuppressionReason(StrEnum):
    UNSUBSCRIBED = "unsubscribed"
    NO_REACHABLE_CHANNEL = "no_reachable_channel"
    DUPLICATE = "duplicate"
    COOLDOWN = "cooldown"
    FREQUENCY_CAP = "frequency_cap"
    TEMPLATE_ERROR = "template_error"


class Scope(StrEnum):
    """API key permissions. ``admin`` implies every other scope."""

    EVENTS_WRITE = "events:write"
    MESSAGES_WRITE = "messages:write"
    MESSAGES_READ = "messages:read"
    RECIPIENTS_WRITE = "recipients:write"
    RECIPIENTS_READ = "recipients:read"
    ADMIN = "admin"
