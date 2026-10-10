"""Database schema.

JSON columns are replaced wholesale on update (``obj.field = {...}``) rather than mutated
in place, because SQLAlchemy does not track in-place changes to plain JSON values.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cue_notify.core.enums import (
    BroadcastStatus,
    DigestStatus,
    EventStatus,
    Importance,
    MessageStatus,
    PreferenceSource,
)
from cue_notify.core.ids import token
from cue_notify.core.time import utcnow
from cue_notify.db.base import Base, IdMixin, JSONType, TimestampMixin, enum_type

KEY = 128  # length of human-chosen keys (templates, rules)


class Agent(TimestampMixin, Base):
    """An automated sender (an AI agent, a service) with its own attention budget.

    API keys bound to an agent send on its behalf; Cue then applies the agent's limits on
    top of the recipient's preferences and the category's policy.
    """

    __tablename__ = "agents"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    categories: Mapped[list[str] | None]
    """Categories the agent may send in; ``None`` allows all."""
    max_importance: Mapped[Importance] = mapped_column(
        enum_type(Importance), default=Importance.HIGH
    )
    frequency_caps: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    """Per recipient: at most ``limit`` messages from this agent per ``period``."""
    approval: Mapped[dict[str, Any] | bool | None] = mapped_column(JSONType)
    """JSON Logic (or ``true``); when it holds, a person must approve the message first."""
    approval_ttl_seconds: Mapped[int] = mapped_column(Integer, default=86_400)


class ApiKey(IdMixin, TimestampMixin, Base):
    __tablename__ = "api_keys"

    name: Mapped[str] = mapped_column(String(100), unique=True)
    prefix: Mapped[str] = mapped_column(String(32), unique=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[list[str]]
    last_used_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    agent_key: Mapped[str | None] = mapped_column(
        ForeignKey("agents.key", ondelete="RESTRICT", onupdate="CASCADE")
    )


class Recipient(IdMixin, TimestampMixin, Base):
    """Someone who can receive messages, identified by *your* system's id (``external_id``)."""

    __tablename__ = "recipients"

    external_id: Mapped[str] = mapped_column(String(255), unique=True)
    locale: Mapped[str | None] = mapped_column(String(35))
    timezone: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    preferences: Mapped[dict[str, Any]] = mapped_column(default=dict)
    preference_token: Mapped[str] = mapped_column(String(64), unique=True, default=token)
    """Lets the person manage their own preferences (hosted page, unsubscribe links)."""

    addresses: Mapped[list[Address]] = relationship(
        back_populates="recipient",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
        order_by="Address.created_at",
    )


class Address(IdMixin, TimestampMixin, Base):
    """A destination on one channel: a device token, phone number, e-mail, chat id or URL."""

    __tablename__ = "addresses"
    __table_args__ = (UniqueConstraint("recipient_id", "channel", "value"),)

    recipient_id: Mapped[UUID] = mapped_column(
        ForeignKey("recipients.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(String(2048))
    label: Mapped[str | None] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    disabled_reason: Mapped[str | None] = mapped_column(String(255))

    recipient: Mapped[Recipient] = relationship(back_populates="addresses")


class Category(TimestampMixin, Base):
    """A class of messages (``transactional``, ``marketing``…) that carries delivery policy."""

    __tablename__ = "categories"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str | None] = mapped_column(Text)
    frequency_caps: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    respect_quiet_hours: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_unsubscribe: Mapped[bool] = mapped_column(Boolean, default=True)
    fatigue: Mapped[dict[str, Any] | None]


class Template(IdMixin, TimestampMixin, Base):
    __tablename__ = "templates"

    key: Mapped[str] = mapped_column(String(KEY), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    default_locale: Mapped[str] = mapped_column(String(35))
    locales: Mapped[dict[str, Any]]
    ai_instructions: Mapped[str | None] = mapped_column(Text)
    ai_attributes: Mapped[list[str]] = mapped_column(default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012 — optimistic locking


class Rule(IdMixin, TimestampMixin, Base):
    """When an event named ``event`` arrives and ``condition`` holds, send ``template``."""

    __tablename__ = "rules"

    key: Mapped[str] = mapped_column(String(KEY), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    event: Mapped[str] = mapped_column(String(255), index=True)
    condition: Mapped[dict[str, Any] | None]
    template_key: Mapped[str] = mapped_column(
        ForeignKey("templates.key", ondelete="RESTRICT", onupdate="CASCADE")
    )
    category_key: Mapped[str] = mapped_column(
        ForeignKey("categories.key", ondelete="RESTRICT", onupdate="CASCADE")
    )
    channels: Mapped[list[str]]
    priority: Mapped[int] = mapped_column(Integer, default=0)
    stop_processing: Mapped[bool] = mapped_column(Boolean, default=True)
    delay_seconds: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_seconds: Mapped[int | None] = mapped_column(Integer)
    rollout_percent: Mapped[float] = mapped_column(Float, default=100.0)
    digest: Mapped[dict[str, Any] | None]
    importance: Mapped[Importance] = mapped_column(enum_type(Importance), default=Importance.NORMAL)
    expires_after_seconds: Mapped[int | None] = mapped_column(Integer)


class Event(IdMixin, Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("source", "idempotency_key"),)

    source: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(255), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    recipient_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("recipients.id", ondelete="SET NULL"), index=True
    )
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    occurred_at: Mapped[datetime]
    received_at: Mapped[datetime]
    status: Mapped[EventStatus] = mapped_column(enum_type(EventStatus), default=EventStatus.PENDING)
    outcome: Mapped[list[dict[str, Any]] | None]
    error: Mapped[str | None] = mapped_column(Text)


class Message(IdMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_caps", "recipient_id", "category_key", "send_at"),
        Index("ix_messages_cooldown", "recipient_id", "rule_id", "send_at"),
        Index("ix_messages_agent", "recipient_id", "agent_key", "send_at"),
    )

    recipient_id: Mapped[UUID] = mapped_column(ForeignKey("recipients.id", ondelete="CASCADE"))
    event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), index=True
    )
    rule_id: Mapped[UUID | None] = mapped_column(ForeignKey("rules.id", ondelete="SET NULL"))
    broadcast_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("broadcasts.id", ondelete="SET NULL"), index=True
    )
    agent_key: Mapped[str | None] = mapped_column(String(64))
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    """Name of the API key that approved or rejected the message."""
    reviewed_at: Mapped[datetime | None]
    # Snapshots, not foreign keys: message history must survive template/category edits.
    template_key: Mapped[str | None] = mapped_column(String(KEY))
    template_version: Mapped[int | None] = mapped_column(Integer)
    category_key: Mapped[str] = mapped_column(String(64))

    channels: Mapped[list[str]] = mapped_column(default=list)
    channel: Mapped[str | None] = mapped_column(String(64))
    locale: Mapped[str | None] = mapped_column(String(35))
    content: Mapped[dict[str, Any]] = mapped_column(default=dict)
    importance: Mapped[Importance] = mapped_column(enum_type(Importance), default=Importance.NORMAL)
    personalize: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_context: Mapped[dict[str, Any] | None]
    generated_by: Mapped[str] = mapped_column(String(128), default="template")
    ai_tokens: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[MessageStatus] = mapped_column(enum_type(MessageStatus), index=True)
    status_reason: Mapped[str | None] = mapped_column(String(500))
    dedup_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    tracking_token: Mapped[str] = mapped_column(String(64), unique=True, default=token)

    send_at: Mapped[datetime]
    expires_at: Mapped[datetime | None]
    """Not worth delivering after this (a one-time code, "your driver is here")."""
    sent_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    opened_at: Mapped[datetime | None]
    clicked_at: Mapped[datetime | None]
    converted_at: Mapped[datetime | None]
    conversion_value: Mapped[float | None] = mapped_column(Float)

    recipient: Mapped[Recipient] = relationship(lazy="raise")


class Broadcast(IdMixin, TimestampMixin, Base):
    """One message sent to an audience, processed in resumable batches."""

    __tablename__ = "broadcasts"

    name: Mapped[str] = mapped_column(String(255))
    template_key: Mapped[str] = mapped_column(
        ForeignKey("templates.key", ondelete="RESTRICT", onupdate="CASCADE")
    )
    category_key: Mapped[str] = mapped_column(
        ForeignKey("categories.key", ondelete="RESTRICT", onupdate="CASCADE")
    )
    channels: Mapped[list[str]]
    audience: Mapped[dict[str, Any]]
    variables: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[BroadcastStatus] = mapped_column(
        enum_type(BroadcastStatus), default=BroadcastStatus.DRAFT
    )
    scheduled_at: Mapped[datetime | None]
    cursor: Mapped[str | None] = mapped_column(String(64))
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    completed_at: Mapped[datetime | None]


class Digest(IdMixin, TimestampMixin, Base):
    """Events grouped by a digest rule, waiting to become one message."""

    __tablename__ = "digests"
    __table_args__ = (
        # At most one open digest per rule, recipient and grouping key.
        Index(
            "uq_digests_open",
            "rule_id",
            "recipient_id",
            "key",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )

    rule_id: Mapped[UUID] = mapped_column(ForeignKey("rules.id", ondelete="CASCADE"))
    recipient_id: Mapped[UUID] = mapped_column(
        ForeignKey("recipients.id", ondelete="CASCADE"), index=True
    )
    key: Mapped[str] = mapped_column(String(255))
    status: Mapped[DigestStatus] = mapped_column(enum_type(DigestStatus), default=DigestStatus.OPEN)
    items: Mapped[list[dict[str, Any]]] = mapped_column(default=list)
    count: Mapped[int] = mapped_column(Integer, default=0)
    closes_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]
    message_id: Mapped[UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))


class PreferenceChange(IdMixin, Base):
    """Consent ledger: one row per change to a recipient's preferences."""

    __tablename__ = "preference_changes"

    recipient_id: Mapped[UUID] = mapped_column(
        ForeignKey("recipients.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[PreferenceSource] = mapped_column(enum_type(PreferenceSource))
    changes: Mapped[dict[str, Any]]
    """``{"unsubscribed": [...], "resubscribed": [...], "muted": [...], "unmuted": [...],
    "quiet_hours": {...}}`` — only the keys that changed."""
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Job(IdMixin, Base):
    """A unit of background work. See :mod:`cue_notify.queue`."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_ready", "dead", "run_at"),)

    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    key: Mapped[str | None] = mapped_column(String(255), unique=True)
    run_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer)
    locked_until: Mapped[datetime | None]
    locked_by: Mapped[str | None] = mapped_column(String(255))
    last_error: Mapped[str | None] = mapped_column(Text)
    dead: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime]


class AiUsage(Base):
    """Daily AI token consumption, used to enforce ``ai.daily_token_budget``."""

    __tablename__ = "ai_usage"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
