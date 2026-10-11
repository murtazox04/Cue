"""Provider-agnostic AI copy personalisation contract and guardrails.

The AI layer is deliberately narrow: it may *rephrase* the title and body of a message
that a template already rendered. It never chooses recipients, links, images or payload
data, and its output must preserve every number and URL of the original text — an LLM
must not be able to change "1 250 000" into "1 500 000" in a payment reminder.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from cue_notify.core.content import Content

_URL = re.compile(r"https?://[^\s<>\"']+")
# Links without a scheme ("pay.example/login") count too, or a model could invent one.
_DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b(?:/[^\s<>\"']*)?", re.IGNORECASE)
# Dates, times and ranges ("2026-10-11", "08:00") are one fact, so their parts cannot be
# swapped. \s also matches no-break spaces.
_NUMBER = re.compile(r"\d(?:[\d\s.,'/:\-]*\d)?")
_SEPARATORS = re.compile(r"[^\d]+")


class GenerationError(Exception):
    """Personalisation failed; callers fall back to the template's text."""


class Copy(BaseModel):
    """What the model is asked to produce."""

    title: str | None = Field(default=None, description="Short headline, or null if none.")
    body: str = Field(description="The message text.")


@dataclass(frozen=True, slots=True)
class PersonalizationRequest:
    baseline: Content
    locale: str
    channel: str
    instructions: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)
    """Recipient attributes explicitly allow-listed by the template (never the whole profile)."""

    @property
    def max_title(self) -> int:
        return max(len(self.baseline.title or ""), 40) + 25

    @property
    def max_body(self) -> int:
        return max(int(len(self.baseline.body) * 1.5), 160)


@dataclass(frozen=True, slots=True)
class PersonalizationResult:
    copy: Copy
    model: str
    tokens: int

    def apply(self, baseline: Content) -> Content:
        return baseline.model_copy(update={"title": self.copy.title, "body": self.copy.body})


class ContentGenerator(Protocol):
    @property
    def model_name(self) -> str: ...

    async def personalize(self, request: PersonalizationRequest) -> PersonalizationResult: ...

    async def aclose(self) -> None: ...


def facts(text: str | None) -> set[str]:
    """Numbers, links and domains that must survive a rewrite.

    A number may change its separators ("1 250 000" and "1,250,000" are the same fact)
    but not where they are, so "1 250 000" cannot become "12 500.00".
    """
    if not text:
        return set()
    urls = set(_URL.findall(text))
    rest = _URL.sub(" ", text)
    domains = {match.lower() for match in _DOMAIN.findall(rest)}
    rest = _DOMAIN.sub(" ", rest)
    numbers = {_SEPARATORS.sub("|", match) for match in _NUMBER.findall(rest)}
    return urls | domains | numbers


def violations(request: PersonalizationRequest, copy: Copy) -> list[str]:
    """Explain why ``copy`` is unacceptable for ``request`` (empty list = acceptable)."""
    problems: list[str] = []
    required = facts(request.baseline.title) | facts(request.baseline.body)
    produced = facts(copy.title) | facts(copy.body)
    missing = sorted(required - produced)
    if missing:
        problems.append(
            "keep these numbers and links exactly as in the original: " + ", ".join(missing)
        )
    invented = sorted(produced - required - {""})
    if invented:
        problems.append(
            "do not introduce numbers or links that are not in the original: " + ", ".join(invented)
        )
    if request.baseline.title and not copy.title:
        problems.append("a title is required")
    if copy.title and len(copy.title) > request.max_title:
        problems.append(f"title must be at most {request.max_title} characters")
    if len(copy.body) > request.max_body:
        problems.append(f"body must be at most {request.max_body} characters")
    if not copy.body.strip():
        problems.append("body must not be empty")
    return problems
