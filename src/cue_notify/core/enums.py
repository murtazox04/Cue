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
    """Withdrawn before sending (e.g. its broadcast was cancelled, or a reviewer rejected it)."""
    PENDING_APPROVAL = "pending_approval"
    """Held until someone approves it (agent policy); not sent, not yet counted."""

    @property
    def is_final(self) -> bool:
        return self not in (
            MessageStatus.QUEUED,
            MessageStatus.SENDING,
            MessageStatus.PENDING_APPROVAL,
        )


#: Statuses that count towards frequency caps and cooldowns.
COUNTED_STATUSES = (
    MessageStatus.QUEUED,
    MessageStatus.SENDING,
    MessageStatus.SENT,
    MessageStatus.DELIVERED,
)


class Importance(StrEnum):
    """How much a message deserves the person's attention right now.

    Devices map it to their own levels (iOS interruption levels, Android priority) and
    Cue's policy uses it: only ``critical`` may break through quiet hours and frequency
    caps, and only in categories people cannot unsubscribe from.
    """

    LOW = "low"
    """Informational: deliver quietly, never interrupt."""
    NORMAL = "normal"
    HIGH = "high"
    """Time-sensitive: worth interrupting for, within the person's quiet hours."""
    CRITICAL = "critical"
    """Security or safety: may break through quiet hours and caps."""

    @property
    def rank(self) -> int:
        return list(Importance).index(self)


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


class PreferenceSource(StrEnum):
    """Who changed a recipient's preferences (kept in the consent ledger)."""

    API = "api"
    """Your backend, through ``PUT /v1/recipients/{id}/preferences``."""
    SELF_SERVICE = "self_service"
    """The person, through the hosted page or your UI calling ``/v1/preferences/{token}``."""
    ONE_CLICK = "one_click"
    """The person's mail client, through an RFC 8058 one-click unsubscribe."""
    SMS_KEYWORD = "sms_keyword"
    """The person replied STOP (or START) to a text message."""


class DigestStatus(StrEnum):
    OPEN = "open"
    """Collecting events until ``closes_at``."""
    SENT = "sent"
    """Composed into a message (which may itself have been suppressed by policy)."""
    CANCELLED = "cancelled"
    """Discarded, e.g. because its rule was disabled."""


class SuppressionReason(StrEnum):
    UNSUBSCRIBED = "unsubscribed"
    NO_REACHABLE_CHANNEL = "no_reachable_channel"
    DUPLICATE = "duplicate"
    COOLDOWN = "cooldown"
    FREQUENCY_CAP = "frequency_cap"
    TEMPLATE_ERROR = "template_error"
    EXPIRED = "expired"
    AGENT_BUDGET = "agent_budget"
    FATIGUE = "fatigue"
    COMPLIANCE = "compliance"
    CONSENT_EXPIRED = "consent_expired"


class Scope(StrEnum):
    """API key permissions. ``admin`` implies every other scope."""

    EVENTS_WRITE = "events:write"
    MESSAGES_WRITE = "messages:write"
    MESSAGES_READ = "messages:read"
    RECIPIENTS_WRITE = "recipients:write"
    RECIPIENTS_READ = "recipients:read"
    ADMIN = "admin"


class ApprovalStatus(StrEnum):
    """Where a request for a person's decision stands."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    """Nobody decided before ``expires_at``; treat it as a no."""
