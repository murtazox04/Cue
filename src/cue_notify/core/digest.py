"""Digest policy: collapse a burst of matching events into one message.

A rule with a digest does not message the recipient for every event. The first matching
event opens a window; later events with the same grouping key join it; when the window
closes (or enough events arrived) one message is composed with every grouped event
available to the template::

    {{ data.count }} new comments on "{{ data.last.post_title }}"
    {% for e in data.events[:3] %}{{ e.author }}{% if not loop.last %}, {% endif %}{% endfor %}
"""

from __future__ import annotations

from datetime import timedelta
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cue_notify.core import templating

MAX_WINDOW = timedelta(days=7)


class DigestPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    window: timedelta = Field(
        description="How long to collect events after the first one, e.g. 'PT10M' or 600.",
        examples=["PT10M"],
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
        if not timedelta(seconds=1) <= self.window <= MAX_WINDOW:
            raise ValueError("window must be between 1 second and 7 days")
        if self.key:
            try:
                templating.check(self.key)
            except templating.RenderError as exc:
                raise ValueError(f"key: {exc}") from exc
        if self.summarize and not self.summary_fields:
            raise ValueError("summarize requires summary_fields (the data the AI may see)")
        return self
