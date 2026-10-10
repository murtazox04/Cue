"""Request and response models of the public HTTP API."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Self
from uuid import UUID

from pydantic import (
    AliasChoices,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from cue_notify.core import logic
from cue_notify.core.content import LocalizedTemplate
from cue_notify.core.digest import DigestPolicy
from cue_notify.core.enums import (
    BroadcastStatus,
    DigestStatus,
    EventStatus,
    Importance,
    MessageStatus,
    PreferenceSource,
    Scope,
)
from cue_notify.core.policy import Fatigue, FrequencyCap, RecipientPreferences
from cue_notify.services.broadcasts import Audience
from cue_notify.services.recipients import AddressIn, RecipientIn

KEY_PATTERN = r"^[a-z0-9][a-z0-9_.\-]*$"
Key = Field(min_length=1, max_length=128, pattern=KEY_PATTERN)


class Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = Field(
        description="Pass as `cursor` to fetch the next page; null on the last page."
    )


# --------------------------------------------------------------------------- recipients


class AddressOut(Model):
    channel: str
    value: str
    label: str | None
    enabled: bool
    disabled_reason: str | None
    created_at: datetime


class RecipientOut(Model):
    external_id: str
    locale: str | None
    timezone: str | None
    attributes: dict[str, Any]
    preferences: RecipientPreferences
    preference_token: str = Field(
        description=(
            "Lets the person manage their own preferences (`/v1/preferences/{token}`, the "
            "hosted page). Treat it like a password-reset link."
        )
    )
    addresses: list[AddressOut]
    created_at: datetime
    updated_at: datetime


class PreferenceChangeOut(Model):
    id: UUID
    source: PreferenceSource
    changes: dict[str, Any] = Field(
        examples=[{"unsubscribed": ["marketing"], "muted": ["sms"]}],
        description="Only what changed: unsubscribed, resubscribed, muted, unmuted, quiet_hours.",
    )
    created_at: datetime


class RecipientUpsert(BaseModel):
    locale: str | None = Field(default=None, max_length=35)
    timezone: str | None = Field(default=None, max_length=64)
    attributes: dict[str, Any] | None = None
    addresses: list[AddressIn] | None = None


# --------------------------------------------------------------------------- events


class EventAccepted(BaseModel):
    id: UUID
    status: EventStatus
    duplicate: bool = Field(description="True when the idempotency key was already seen.")


class EventOut(Model):
    id: UUID
    name: str
    source: str
    idempotency_key: str | None
    data: dict[str, Any]
    status: EventStatus
    outcome: list[dict[str, Any]] | None = Field(description="Per-rule evaluation results.")
    error: str | None
    occurred_at: datetime
    received_at: datetime


# --------------------------------------------------------------------------- categories


class CategoryFields(BaseModel):
    description: str | None = None
    frequency_caps: list[FrequencyCap] = Field(
        default_factory=list,
        examples=[[{"period": "P1D", "limit": 3}, {"period": "P7D", "limit": 10}]],
    )
    respect_quiet_hours: bool = True
    allow_unsubscribe: bool = True
    fatigue: Fatigue | None = Field(
        default=None,
        description="Pause low and normal messages to people who ignore this category.",
        examples=[{"after": 5, "pause": "P7D"}],
    )


class CategoryIn(CategoryFields):
    key: str = Field(min_length=1, max_length=64, pattern=KEY_PATTERN, examples=["marketing"])


class CategoryUpdate(BaseModel):
    description: str | None = None
    frequency_caps: list[FrequencyCap] | None = None
    respect_quiet_hours: bool | None = None
    allow_unsubscribe: bool | None = None
    fatigue: Fatigue | None = Field(default=None, description="Set to null to turn off.")


class CategoryOut(Model, CategoryFields):
    key: str
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- templates


class TemplateFields(BaseModel):
    description: str | None = None
    default_locale: str = Field(default="en", max_length=35)
    locales: dict[str, LocalizedTemplate] = Field(
        min_length=1,
        examples=[
            {
                "en": {
                    "title": "Your order shipped",
                    "body": "Order {{ data.order_id }} is on its way.",
                    "url": "https://shop.example/orders/{{ data.order_id }}",
                    "channels": {"sms": {"body": "Order {{ data.order_id }} shipped."}},
                }
            }
        ],
    )
    ai_instructions: str | None = Field(
        default=None,
        description="When set (and AI is enabled), the copy is personalised with this guidance.",
    )
    ai_attributes: list[str] = Field(
        default_factory=list,
        description="Recipient attributes the AI may see. Nothing else leaves your servers.",
    )

    @model_validator(mode="after")
    def _default_locale_exists(self) -> Self:
        if self.default_locale not in self.locales:
            raise ValueError(f"default_locale {self.default_locale!r} is not in locales")
        return self


class TemplateIn(TemplateFields):
    key: str = Key


class TemplateOut(Model):
    key: str
    description: str | None
    default_locale: str
    locales: dict[str, LocalizedTemplate]
    ai_instructions: str | None
    ai_attributes: list[str]
    version: int
    created_at: datetime
    updated_at: datetime


class PreviewIn(BaseModel):
    locale: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    recipient: str | None = Field(default=None, description="External id to render for.")


class PreviewOut(BaseModel):
    locale: str
    variants: dict[str, dict[str, Any]]


# --------------------------------------------------------------------------- rules


def check_condition(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is not None:
        try:
            logic.validate(value)
        except logic.LogicError as exc:
            raise ValueError(str(exc)) from exc
    return value


class RuleFields(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    enabled: bool = True
    event: str = Field(
        min_length=1,
        max_length=255,
        description="Event name to react to; shell-style wildcards allowed (`order.*`).",
        examples=["order.shipped"],
    )
    condition: dict[str, Any] | None = Field(
        default=None,
        description="JSON Logic over `{event, recipient}`. Null matches every event.",
        examples=[{">": [{"var": "event.data.total"}, 100]}],
    )
    template: str = Key
    category: str = Field(min_length=1, max_length=64)
    channels: list[str] = Field(min_length=1, description="Channels in fallback order.")
    priority: int = Field(default=0, description="Higher runs first.")
    stop_processing: bool = Field(
        default=True, description="Skip lower-priority rules once this one matches."
    )
    delay_seconds: int = Field(default=0, ge=0, le=31_536_000)
    cooldown_seconds: int | None = Field(
        default=None, ge=1, description="Minimum gap between this rule's messages to a recipient."
    )
    rollout_percent: float = Field(default=100.0, ge=0, le=100)
    importance: Importance = Field(
        default=Importance.NORMAL,
        description="How much these messages deserve attention; see the docs on importance.",
    )
    expires_after_seconds: int | None = Field(
        default=None,
        ge=1,
        le=31_536_000,
        description="Drop the message if it cannot be delivered within this time.",
    )
    digest: DigestPolicy | None = Field(
        default=None,
        description="Group matching events into one message per window instead of one each.",
        examples=[{"window": "PT10M", "key": "{{ event.data.post_id }}", "send_after": 20}],
    )

    @field_validator("condition")
    @classmethod
    def _valid_logic(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return check_condition(value)


class RuleIn(RuleFields):
    key: str = Key


class RuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    enabled: bool | None = None
    event: str | None = Field(default=None, min_length=1, max_length=255)
    condition: dict[str, Any] | None = None
    template: str | None = None
    category: str | None = None
    channels: list[str] | None = Field(default=None, min_length=1)
    priority: int | None = None
    stop_processing: bool | None = None
    delay_seconds: int | None = Field(default=None, ge=0, le=31_536_000)
    cooldown_seconds: int | None = Field(default=None, ge=1)
    rollout_percent: float | None = Field(default=None, ge=0, le=100)
    importance: Importance | None = None
    expires_after_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    digest: DigestPolicy | None = Field(default=None, description="Set to null to turn off.")

    @field_validator("condition")
    @classmethod
    def _valid_logic(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return check_condition(value)


class RuleOut(Model):
    key: str
    name: str
    description: str | None
    enabled: bool
    event: str
    condition: dict[str, Any] | None
    template: str = Field(validation_alias="template_key")
    category: str = Field(validation_alias="category_key")
    channels: list[str]
    priority: int
    stop_processing: bool
    delay_seconds: int
    cooldown_seconds: int | None
    rollout_percent: float
    importance: Importance
    expires_after_seconds: int | None
    digest: DigestPolicy | None
    created_at: datetime
    updated_at: datetime


class EvaluateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    recipient: str = Field(description="External id of an existing recipient.")
    data: dict[str, Any] = Field(default_factory=dict)


class RuleOutcomeOut(BaseModel):
    rule: str
    matched: bool
    status: MessageStatus | None = None
    reason: str | None = None
    digest_id: UUID | None = None


# --------------------------------------------------------------------------- digests


class DigestOut(Model):
    id: UUID
    rule_id: UUID
    key: str
    status: DigestStatus
    count: int
    items: list[dict[str, Any]]
    closes_at: datetime
    sent_at: datetime | None
    message_id: UUID | None
    created_at: datetime


# --------------------------------------------------------------------------- messages


class SendIn(BaseModel):
    recipient: str | RecipientIn
    template: str
    category: str = "transactional"
    channels: list[str] = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    send_at: AwareDatetime | None = None
    importance: Importance = Importance.NORMAL
    expires_at: AwareDatetime | None = Field(
        default=None, description="Drop the message if it is not delivered by then."
    )
    idempotency_key: str | None = Field(default=None, max_length=200)


class MessageOut(Model):
    id: UUID
    status: MessageStatus
    status_reason: str | None
    category: str = Field(validation_alias="category_key")
    template: str | None = Field(validation_alias="template_key")
    template_version: int | None
    channels: list[str]
    channel: str | None
    locale: str | None
    content: dict[str, dict[str, Any]]
    importance: Importance
    generated_by: str
    event_id: UUID | None
    rule_id: UUID | None
    broadcast_id: UUID | None
    agent: str | None = Field(validation_alias="agent_key")
    reviewed_by: str | None
    reviewed_at: datetime | None
    attempts: int
    provider_message_id: str | None
    send_at: datetime
    expires_at: datetime | None
    sent_at: datetime | None
    delivered_at: datetime | None
    opened_at: datetime | None
    clicked_at: datetime | None
    converted_at: datetime | None
    created_at: datetime


class ReviewIn(BaseModel):
    reason: str | None = Field(default=None, max_length=400)


# --------------------------------------------------------------------------- agents


class AgentFields(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    enabled: bool = True
    categories: list[str] | None = Field(
        default=None, description="Categories it may send in. Null allows all."
    )
    max_importance: Importance = Field(
        default=Importance.HIGH, description="Higher importance is lowered to this."
    )
    frequency_caps: list[FrequencyCap] = Field(
        default_factory=lambda: [FrequencyCap(period=timedelta(days=1), limit=5)],
        description="Its attention budget: messages per recipient per period.",
        examples=[[{"period": "P1D", "limit": 5}]],
    )
    approval: dict[str, Any] | bool | None = Field(
        default=None,
        description=(
            "JSON Logic over `{message, recipient, data}`; when it holds, the message waits "
            "for approval. `true` reviews everything, null nothing."
        ),
        examples=[{"in": [{"var": "message.importance"}, ["high", "critical"]]}],
    )
    approval_ttl_seconds: int = Field(
        default=86_400, ge=60, le=604_800, description="Unreviewed messages expire after this."
    )

    @field_validator("approval")
    @classmethod
    def _valid_logic(cls, value: dict[str, Any] | bool | None) -> dict[str, Any] | bool | None:
        return value if isinstance(value, bool) else check_condition(value)


class AgentIn(AgentFields):
    key: str = Field(min_length=1, max_length=64, pattern=KEY_PATTERN, examples=["support-bot"])


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    enabled: bool | None = None
    categories: list[str] | None = None
    max_importance: Importance | None = None
    frequency_caps: list[FrequencyCap] | None = None
    approval: dict[str, Any] | bool | None = None
    approval_ttl_seconds: int | None = Field(default=None, ge=60, le=604_800)

    @field_validator("approval")
    @classmethod
    def _valid_logic(cls, value: dict[str, Any] | bool | None) -> dict[str, Any] | bool | None:
        return value if isinstance(value, bool) else check_condition(value)


class AgentOut(Model, AgentFields):
    key: str
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- broadcasts


class BroadcastIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    template: str
    category: str = "marketing"
    channels: list[str] = Field(min_length=1)
    audience: Audience
    variables: dict[str, Any] = Field(default_factory=dict)


class StartIn(BaseModel):
    at: AwareDatetime | None = Field(default=None, description="Start time; now when omitted.")


class BroadcastOut(Model):
    id: UUID
    name: str
    template: str = Field(validation_alias="template_key")
    category: str = Field(validation_alias="category_key")
    channels: list[str]
    audience: dict[str, Any]
    variables: dict[str, Any]
    status: BroadcastStatus
    stats: dict[str, int]
    error: str | None = Field(description="Why the broadcast failed, if it did.")
    scheduled_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


# --------------------------------------------------------------------------- api keys


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Scope] = Field(min_length=1)
    expires_at: AwareDatetime | None = None
    agent: str | None = Field(
        default=None, description="Bind the key to an agent: its sends follow that agent's policy."
    )


class ApiKeyOut(Model):
    id: UUID
    name: str
    prefix: str
    scopes: list[Scope]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    agent: str | None = Field(validation_alias=AliasChoices("agent_key", "agent"))


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="The secret. Store it now — it cannot be shown again.")
