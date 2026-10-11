"""Screening: a fast, typed second opinion on what an agent is about to send.

An agent picks its own words and its own importance. Before such a message reaches a
person, a decision model reads it and answers a few narrow questions: how urgent it
really is, whether it pressures or deceives the reader, whether it leaks a secret. Code
here, not the model, decides what to do with the answers:

- any risk above ``hold_above`` holds the message for a person to approve;
- an importance the model confidently judges lower is lowered (never raised);
- a ``critical`` claim the model does not confirm is held, because ``critical`` has
  already skipped quiet hours and caps.

The default backend is TypeSafe's Jev, a "System One" model that returns calibrated
probabilities instead of text in a few hundred milliseconds, for a fraction of a cent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from cue_notify.core.enums import Importance
from cue_notify.settings import ScreeningSettings

URGENCY_LEVELS: dict[Importance, str] = {
    Importance.LOW: "Can wait: informational, no action or time pressure",
    Importance.NORMAL: "Worth reading today: useful, but nothing is lost by waiting hours",
    Importance.HIGH: "Time-sensitive: the reader should act within the next few hours",
    Importance.CRITICAL: (
        "Urgent: safety, security, money or account access is at risk and needs action now"
    ),
}

RISKS: dict[str, str] = {
    "pressure": (
        "The message pressures or manipulates the reader, for example with false urgency, "
        "guilt, fear or threats, to push them into an action"
    ),
    "deception": (
        "The message looks deceptive: it impersonates someone, makes misleading claims or "
        "offers, or asks the reader to confirm credentials or payment details"
    ),
    "secret": (
        "The message exposes a password, API key, access token, private key or other "
        "credential in its text"
    ),
}


class ScreeningError(Exception):
    """The screening backend could not give an answer."""


@dataclass(frozen=True, slots=True)
class ScreeningRequest:
    agent: str
    agent_description: str | None
    category: str
    importance: Importance
    content: dict[str, dict[str, Any]]
    """Rendered variants of the message, keyed by variant name."""

    def state(self) -> dict[str, Any]:
        """What the model reads: the sender, the claim and the words, never recipient data."""
        variants: list[dict[str, Any]] = []
        for variant in self.content.values():
            text = {key: variant[key] for key in ("title", "body", "url") if variant.get(key)}
            actions = [
                {"label": action.get("label"), "url": action.get("url")}
                for action in variant.get("actions") or []
            ]
            if actions:
                text["actions"] = actions
            if text and text not in variants:
                variants.append(text)
        return {
            "sender": {"agent": self.agent, "purpose": self.agent_description},
            "category": self.category,
            "claimed_importance": self.importance.value,
            "message": variants,
        }


@dataclass(frozen=True, slots=True)
class Verdict:
    hold: str | None = None
    """Why a person must approve the message first, if they must."""
    importance: Importance | None = None
    """A lower importance to send with, if the claimed one was overstated."""
    record: dict[str, Any] = field(default_factory=dict)
    """What was asked and answered, stored on the message."""


class Screener(Protocol):
    async def screen(self, request: ScreeningRequest) -> Verdict: ...


def judge(
    request: ScreeningRequest,
    answers: dict[str, Any],
    settings: ScreeningSettings,
    model: str,
) -> Verdict:
    """Turn the model's answers into a verdict. Thresholds live here, in code."""
    risks = {name: float(answers[name]["noul"]) for name in RISKS}
    urgency = answers["urgency"]
    levels = list(URGENCY_LEVELS)
    probabilities = {int(k): float(v) for k, v in urgency["probabilities"].items()}
    judged = levels[max(probabilities, key=lambda level: probabilities[level])]
    confidence = float(urgency["confidence"])
    record: dict[str, Any] = {
        "model": model,
        "risks": risks,
        "urgency": {"importance": judged.value, "confidence": confidence},
    }

    flagged = sorted(name for name, p in risks.items() if p >= settings.hold_above)
    if flagged:
        return Verdict(hold=", ".join(flagged), record=record)
    claimed = request.importance
    if claimed is Importance.CRITICAL and (
        judged is not Importance.CRITICAL or confidence < settings.min_confidence
    ):
        return Verdict(hold="critical not confirmed", record=record)
    if confidence >= settings.min_confidence and judged.rank < claimed.rank:
        record["lowered_from"] = claimed.value
        return Verdict(importance=judged, record=record)
    return Verdict(record=record)


class TypeSafeScreener:
    """:class:`Screener` backed by TypeSafe's System One API (``POST /v1/systemone``)."""

    def __init__(self, settings: ScreeningSettings, http: httpx.AsyncClient) -> None:
        if settings.api_key is None:
            raise ScreeningError("screening.api_key is required for the typesafe provider")
        self._settings = settings
        self._http = http
        self._url = f"{settings.base_url.rstrip('/')}/v1/systemone"
        self._headers = {"Authorization": f"Bearer {settings.api_key.get_secret_value()}"}

    @staticmethod
    def questions() -> dict[str, Any]:
        questions: dict[str, Any] = {
            "urgency": {
                "type": "score",
                "instructions": (
                    "How urgent is this message for the person receiving it, judged from its "
                    "content rather than from the importance the sender claims"
                ),
                "criteria": list(URGENCY_LEVELS.values()),
            }
        }
        for name, instructions in RISKS.items():
            questions[name] = {"type": "noul", "instructions": instructions}
        return questions

    async def screen(self, request: ScreeningRequest) -> Verdict:
        body = {
            "model": self._settings.model,
            "state": json.dumps(request.state(), ensure_ascii=False),
            "questions": self.questions(),
        }
        try:
            response = await self._http.post(
                self._url, json=body, headers=self._headers, timeout=self._settings.timeout
            )
        except httpx.HTTPError as exc:
            raise ScreeningError(f"{type(exc).__name__}: {exc}") from exc
        if not response.is_success:
            raise ScreeningError(f"HTTP {response.status_code}: {response.text[:200]}")
        try:
            payload = response.json()
            return judge(
                request,
                payload["answers"],
                self._settings,
                str(payload.get("model", self._settings.model)),
            )
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise ScreeningError(f"unexpected response: {exc}") from exc


def create_screener(settings: ScreeningSettings, http: httpx.AsyncClient) -> Screener | None:
    if not settings.enabled:
        return None
    return TypeSafeScreener(settings, http)
