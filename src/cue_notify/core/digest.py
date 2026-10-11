"""Digest policy: collapse a burst of matching events into one message.

A rule with a digest does not message the recipient for every event. The first matching
event opens a digest; later events with the same grouping key join it; when it closes
(after ``window``, at the next ``deliver_at`` in the recipient's time zone, or once enough
events arrived) one message is composed with every grouped event available to the
template::

    {{ data.count }} new comments on "{{ data.last.post_title }}"
    {% for e in data.events[:3] %}{{ e.author }}{% if not loop.last %}, {% endif %}{% endfor %}
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Literal, Self
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cue_notify.core import templating

MAX_WINDOW = timedelta(days=7)

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class DigestPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    window: timedelta | None = Field(
        default=None,
        description="How long to collect events after the first one, e.g. 'PT10M' or 600.",
        examples=["PT10M"],
    )
    deliver_at: time | None = Field(
        default=None,
        description=(
            "Instead of a window: send at this time of day in the recipient's time zone, "
            "e.g. '08:00' for a morning summary."
        ),
        examples=["08:00"],
    )
    days: list[Weekday] | None = Field(
        default=None,
        min_length=1,
        description="With deliver_at: the days to send on, e.g. ['mon'] for weekly; default daily.",
    )
    key: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Template for the grouping key, rendered per event (e.g. "
            "'{{ event.data.post_id }}'). Events with different keys form separate "
            "digests. Default: one digest per rule and recipient."
        ),
    )
    max_items: int = Field(
        default=50, ge=1, le=500, description="Most recent events kept for the template."
    )
    send_after: int | None = Field(
        default=None,
        ge=2,
        description="Send as soon as this many events have been grouped, before the window ends.",
    )
    summarize: bool = Field(
        default=False,
        description=(
            "Smart mode: let the AI layer summarise the grouped events into the message "
            "(needs AI enabled; falls back to the template text)."
        ),
    )
    summary_fields: list[str] = Field(
        default_factory=list,
        description="Event data keys the AI may see when summarising. Nothing else is shared.",
    )

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if (self.window is None) == (self.deliver_at is None):
            raise ValueError("set either window or deliver_at")
        if self.window is not None and not timedelta(seconds=1) <= self.window <= MAX_WINDOW:
            raise ValueError("window must be between 1 second and 7 days")
        if self.days is not None and self.deliver_at is None:
            raise ValueError("days needs deliver_at")
        if self.key:
            try:
                templating.check(self.key)
            except templating.RenderError as exc:
                raise ValueError(f"key: {exc}") from exc
        if self.summarize and not self.summary_fields:
            raise ValueError("summarize requires summary_fields (the data the AI may see)")
        return self

    def closes_at(self, now: datetime, tz: ZoneInfo) -> datetime:
        """When a digest opened at ``now`` for a recipient in ``tz`` should be sent."""
        if self.window is not None:
            return now + self.window
        assert self.deliver_at is not None
        local = now.astimezone(tz)
        days = set(self.days or WEEKDAYS)
        for offset in range(8):
            date = local.date() + timedelta(days=offset)
            candidate = datetime.combine(date, self.deliver_at, tzinfo=tz)
            if WEEKDAYS[date.weekday()] in days and candidate > local:
                return candidate.astimezone(UTC)
        raise AssertionError("a week always contains an allowed day")  # pragma: no cover
