"""Message content models shared by templates, the composer, channels and the AI layer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cue_notify.core import templating

DEFAULT_VARIANT = "default"


MAX_ACTIONS = 4


class Action(BaseModel):
    """A button or quick action shown with the message (where the channel supports it)."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    label: str = Field(min_length=1, max_length=64)
    url: str | None = Field(default=None, max_length=2048, description="Opened when chosen.")


class ActionTemplate(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    label: str = Field(min_length=1)
    url: str | None = None


class Content(BaseModel):
    """Rendered, channel-agnostic message content.

    Channels use what they support: push uses ``title``/``body``/``image_url``/``data``,
    SMS uses ``body``, email uses ``title`` as the subject and ``html`` when present.
    """

    model_config = ConfigDict(frozen=True)

    title: str | None = Field(default=None, max_length=512)
    body: str = Field(max_length=10_000)
    url: str | None = Field(default=None, max_length=2048, description="Deep link or web URL.")
    image_url: str | None = Field(default=None, max_length=2048)
    thread: str | None = Field(
        default=None,
        max_length=128,
        description="Grouping key: devices stack notifications with the same thread together.",
    )
    html: str | None = Field(default=None, description="Rich body for channels that support it.")
    data: dict[str, str] = Field(
        default_factory=dict, description="Key/value payload delivered alongside (push data)."
    )
    actions: list[Action] = Field(default_factory=list, max_length=MAX_ACTIONS)


class ContentTemplate(BaseModel):
    """Jinja2 sources for one locale. Each field is rendered with the message context."""

    title: str | None = None
    body: str
    url: str | None = None
    image_url: str | None = None
    thread: str | None = None
    html: str | None = None
    data: dict[str, str] = Field(default_factory=dict)
    actions: list[ActionTemplate] = Field(
        default_factory=list,
        max_length=MAX_ACTIONS,
        description="Buttons: `label` and `url` are templates.",
    )

    @model_validator(mode="after")
    def _compiles(self) -> Self:
        for name, source in self._sources():
            try:
                templating.check(source, html=name == "html")
            except templating.RenderError as exc:
                raise ValueError(f"{name}: {exc}") from exc
        return self

    def _sources(self) -> list[tuple[str, str]]:
        fields = [("title", self.title), ("body", self.body), ("url", self.url)]
        fields += [("image_url", self.image_url), ("thread", self.thread), ("html", self.html)]
        sources = [(name, value) for name, value in fields if value]
        sources += [(f"data.{key}", value) for key, value in self.data.items()]
        for action in self.actions:
            sources.append((f"actions.{action.id}.label", action.label))
            if action.url:
                sources.append((f"actions.{action.id}.url", action.url))
        return sources

    def render(self, context: Mapping[str, Any]) -> Content:
        def text(source: str | None) -> str | None:
            return templating.render(source, context) if source else None

        return Content(
            title=text(self.title),
            body=templating.render(self.body, context),
            url=text(self.url),
            image_url=text(self.image_url),
            thread=text(self.thread),
            html=templating.render(self.html, context, html=True) if self.html else None,
            data={key: templating.render(value, context) for key, value in self.data.items()},
            actions=[
                Action(
                    id=action.id,
                    label=templating.render(action.label, context),
                    url=text(action.url),
                )
                for action in self.actions
            ],
        )


class LocalizedTemplate(ContentTemplate):
    """A locale's content plus optional per-channel overrides (e.g. a shorter SMS body).

    Override fields replace the base fields for that channel; unset fields are inherited.
    """

    channels: dict[str, ContentOverride] = Field(default_factory=dict)

    def render_variants(self, context: Mapping[str, Any]) -> dict[str, Content]:
        base = self.render(context)
        variants = {DEFAULT_VARIANT: base}
        for channel, override in self.channels.items():
            merged = self.model_dump(exclude={"channels"}) | override.model_dump(
                exclude_none=True, exclude_unset=True
            )
            variants[channel] = ContentTemplate.model_validate(merged).render(context)
        return variants


class ContentOverride(BaseModel):
    title: str | None = None
    body: str | None = None
    url: str | None = None
    image_url: str | None = None
    thread: str | None = None
    html: str | None = None
    data: dict[str, str] | None = None
    actions: list[ActionTemplate] | None = None


LocalizedTemplate.model_rebuild()
