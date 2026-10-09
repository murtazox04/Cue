from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from cue_notify.ai import Copy, GenerationError, PersonalizationRequest
from cue_notify.ai.agent import PydanticAIGenerator, build_prompt
from cue_notify.ai.base import facts, violations
from cue_notify.core.content import Content
from cue_notify.settings import AISettings

BASELINE = Content(
    title="Payment due",
    body="Pay 1 250 000 so'm by 12.10 at https://pay.test/inv/77",
)
REQUEST = PersonalizationRequest(
    baseline=BASELINE, locale="en", channel="push", context={"first_name": "Ann"}
)


def test_facts_are_separator_insensitive() -> None:
    assert facts("Pay 1 250 000 so'm") == {"1250000"}
    assert facts("Pay 1,250,000") == {"1250000"}
    assert "https://pay.test/inv/77" in facts(BASELINE.body)


def test_violations() -> None:
    good = Copy(
        title="Ann, payment due", body="Ann, 1,250,000 so'm due 12.10: https://pay.test/inv/77"
    )
    assert violations(REQUEST, good) == []

    changed = Copy(title="Due", body="Pay 1 500 000 so'm by 12.10 at https://pay.test/inv/77")
    problems = violations(REQUEST, changed)
    assert any("keep these numbers" in p for p in problems)
    assert any("do not introduce" in p for p in problems)

    assert any(
        "title is required" in p for p in violations(REQUEST, Copy(title=None, body=BASELINE.body))
    )
    too_long = Copy(title="x", body=BASELINE.body + " " * 500 + "!")
    assert any("at most" in p for p in violations(REQUEST, too_long))


def _model(*outputs: dict[str, Any]) -> FunctionModel:
    remaining = list(outputs)

    def respond(_: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, remaining.pop(0))])

    return FunctionModel(respond, model_name="fake")


async def test_generator_retries_until_facts_are_preserved() -> None:
    model = _model(
        {"title": "Ann!", "body": "Pay 2 000 000 now"},  # wrong amount → retried
        {"title": "Ann, payment due", "body": "1 250 000 so'm by 12.10 — https://pay.test/inv/77"},
    )
    generator = PydanticAIGenerator(AISettings(model="test"), model=model)
    result = await generator.personalize(REQUEST)
    assert result.copy.title == "Ann, payment due"
    assert result.tokens > 0
    personalised = result.apply(BASELINE)
    assert personalised.body.startswith("1 250 000")


async def test_generator_gives_up_when_model_keeps_breaking_rules() -> None:
    bad = {"title": "x", "body": "Pay 9"}
    generator = PydanticAIGenerator(AISettings(model="test"), model=_model(bad, bad, bad, bad))
    with pytest.raises(GenerationError):
        await generator.personalize(REQUEST)


def test_prompt_contains_only_allow_listed_context() -> None:
    prompt = build_prompt(REQUEST)
    assert "<locale>en</locale>" in prompt
    assert '"first_name": "Ann"' in prompt
    assert "1 250 000" in prompt


def test_ai_settings_require_a_model() -> None:
    with pytest.raises(ValueError, match=r"ai\.model is required"):
        AISettings(enabled=True)
