"""Screening: turning a decision model's answers into what Cue does."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from cue_notify.ai.screening import (
    RISKS,
    ScreeningError,
    ScreeningRequest,
    TypeSafeScreener,
    judge,
)
from cue_notify.core.enums import Importance
from cue_notify.settings import ScreeningSettings

SETTINGS = ScreeningSettings(enabled=True, api_key="ts-test")


def _request(importance: Importance = Importance.NORMAL) -> ScreeningRequest:
    return ScreeningRequest(
        agent="support-bot",
        agent_description="Answers support tickets",
        category="transactional",
        importance=importance,
        content={
            "default": {"title": "Your refund", "body": "Refund of 40 EUR sent.", "url": None},
            "sms": {"body": "Refund of 40 EUR sent."},
            "push": {"title": "Your refund", "body": "Refund of 40 EUR sent."},
        },
    )


def _answers(level: int, confidence: float = 0.9, **risks: float) -> dict[str, Any]:
    probabilities = {str(i): 0.0 for i in range(4)}
    probabilities[str(level)] = 1.0
    answers: dict[str, Any] = {
        "urgency": {"type": "score", "probabilities": probabilities, "confidence": confidence}
    }
    for name in RISKS:
        answers[name] = {"type": "noul", "noul": risks.get(name, 0.02)}
    return answers


def test_risky_messages_are_held() -> None:
    verdict = judge(_request(), _answers(1, pressure=0.8, secret=0.6), SETTINGS, "jev")
    assert verdict.hold == "pressure, secret"
    assert verdict.record["risks"]["pressure"] == 0.8


def test_overstated_importance_is_lowered_only_when_the_model_is_sure() -> None:
    sure = judge(_request(Importance.HIGH), _answers(0), SETTINGS, "jev")
    assert (sure.hold, sure.importance) == (None, Importance.LOW)
    assert sure.record["lowered_from"] == "high"
    unsure = judge(_request(Importance.HIGH), _answers(0, confidence=0.3), SETTINGS, "jev")
    assert unsure.importance is None


def test_importance_is_never_raised() -> None:
    verdict = judge(_request(Importance.LOW), _answers(3), SETTINGS, "jev")
    assert (verdict.hold, verdict.importance) == (None, None)


def test_unconfirmed_critical_is_held() -> None:
    assert judge(_request(Importance.CRITICAL), _answers(2), SETTINGS, "jev").hold == (
        "critical not confirmed"
    )
    confirmed = judge(_request(Importance.CRITICAL), _answers(3), SETTINGS, "jev")
    assert confirmed.hold is None


def test_the_model_sees_the_words_not_the_recipient() -> None:
    state = _request().state()
    assert state["sender"] == {"agent": "support-bot", "purpose": "Answers support tickets"}
    assert state["claimed_importance"] == "normal"
    # Identical variants are sent once.
    assert state["message"] == [
        {"title": "Your refund", "body": "Refund of 40 EUR sent."},
        {"body": "Refund of 40 EUR sent."},
    ]


async def test_typesafe_requests_and_answers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": _answers(1)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        verdict = await TypeSafeScreener(SETTINGS, http).screen(_request())
    assert verdict.hold is None
    assert verdict.record["model"] == "jev-1.13.0"
    sent = seen[0]
    assert str(sent.url) == "https://api.typesafe.ai/v1/systemone"
    assert sent.headers["Authorization"] == "Bearer ts-test"
    body = json.loads(sent.content)
    assert body["model"] == "jev-latest"
    assert set(body["questions"]) == {"urgency", *RISKS}
    assert body["questions"]["urgency"]["type"] == "score"
    assert len(body["questions"]["urgency"]["criteria"]) == 4
    assert json.loads(body["state"])["sender"]["agent"] == "support-bot"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, json={"detail": "slow down"}),
        httpx.Response(200, json={"answers": {}}),
        httpx.Response(200, text="not json"),
    ],
)
async def test_typesafe_failures_raise(response: httpx.Response) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(ScreeningError):
            await TypeSafeScreener(SETTINGS, http).screen(_request())


def test_settings_need_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ValidationError, match="TYPESAFE_API_KEY"):
        ScreeningSettings(enabled=True)
    monkeypatch.setenv("TYPESAFE_API_KEY", "from-env")
    settings = ScreeningSettings(enabled=True)
    assert settings.api_key is not None
    assert settings.api_key.get_secret_value() == "from-env"
    assert ScreeningSettings().api_key is None  # disabled: nothing is read


@pytest.mark.parametrize("bad", [float("nan"), 1.5, -0.1, "high"])
async def test_nonsense_scores_count_as_errors(bad: Any) -> None:
    answers = _answers(1)
    answers["deception"]["noul"] = bad

    def handler(_: httpx.Request) -> httpx.Response:
        content = json.dumps({"answers": answers, "model": "jev"}, allow_nan=True)
        return httpx.Response(200, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ScreeningError):
            await TypeSafeScreener(SETTINGS, http).screen(_request())


def test_the_model_sees_html_links_and_push_data() -> None:
    request = ScreeningRequest(
        agent="support-bot",
        agent_description=None,
        category="transactional",
        importance=Importance.NORMAL,
        content={
            "default": {"title": "Refund", "body": "Refund sent."},
            "email": {
                "body": "Refund sent.",
                "html": "<p>Refund sent.</p><script>x</script>"
                '<a href="https://evil.example/login">Confirm your card</a>',
            },
            "push": {"body": "Refund sent.", "data": {"url": "https://evil.example/p"}},
        },
    )
    state = json.dumps(request.state())
    assert "Confirm your card" in state
    assert "https://evil.example/login" in state
    assert "https://evil.example/p" in state
    assert "<script>" not in state


def test_the_model_never_sees_preference_or_review_credentials() -> None:
    token = "0123456789abcdef01234567" + "Ab-_" * 10 + "xyz"
    page = f"https://notify.test/preferences/{token}"
    unsubscribe = f"{page}/unsubscribe?category=marketing"
    review = "https://notify.test/review/" + "R" * 43
    request = ScreeningRequest(
        agent="support-bot",
        agent_description=None,
        category="marketing",
        importance=Importance.NORMAL,
        content={
            "default": {"body": f"Manage {page} or {unsubscribe}. Code {token}."},
            "email": {
                "body": "Hi",
                "html": f'<a href="{unsubscribe}">Stop</a> <a href="{review}">Decide</a>',
            },
            "push": {"body": "Hi", "data": {"manage": f"myapp://prefs?t={token}"}},
        },
    )
    state = json.dumps(request.state())
    assert token not in state
    assert "/preferences/" not in state
    assert "/review/" not in state
    assert "[preferences-link]" in state
    assert "[unsubscribe-link]" in state
    assert "[review-link]" in state
    assert "[preference-token]" in state
    assert request.content["default"]["body"] == (
        "Manage [preferences-link] or [unsubscribe-link]. Code [preference-token]."
    )
