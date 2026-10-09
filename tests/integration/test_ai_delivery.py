"""AI personalisation inside the delivery pipeline."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest

from cue_notify.ai import Copy, GenerationError, PersonalizationRequest, PersonalizationResult
from cue_notify.api.app import create_app
from cue_notify.core.enums import Scope
from cue_notify.runtime import Runtime
from cue_notify.services import api_keys
from cue_notify.settings import AISettings, Settings
from tests.helpers import FakeChannel, create_recipient, drain


@dataclass
class FakeGenerator:
    fail: bool = False
    requests: list[PersonalizationRequest] = field(default_factory=list)

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def personalize(self, request: PersonalizationRequest) -> PersonalizationResult:
        self.requests.append(request)
        if self.fail:
            raise GenerationError("provider down")
        name = request.context.get("first_name", "there")
        copy = Copy(title=request.baseline.title, body=f"{name}, {request.baseline.body}")
        return PersonalizationResult(copy=copy, model=self.model_name, tokens=120)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def generator() -> FakeGenerator:
    return FakeGenerator()


@pytest.fixture
async def ai_runtime(
    settings: Settings, push: FakeChannel, generator: FakeGenerator
) -> AsyncIterator[Runtime]:
    settings.ai = AISettings(model="test:fake", daily_token_budget=200)
    runtime = Runtime.create(settings, channels={"push": push}, generator=generator)
    try:
        yield runtime
    finally:
        await runtime.aclose()


@pytest.fixture
async def ai_client(ai_runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    async with ai_runtime.transaction() as session:
        _, raw = await api_keys.create(session, "ai-tests", [Scope.ADMIN])
    app = create_app(runtime=ai_runtime)
    app.state.runtime = ai_runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://cue.test",
        headers={"Authorization": f"Bearer {raw}"},
    ) as client:
        await client.post(
            "/v1/templates",
            json={
                "key": "welcome",
                "locales": {"en": {"title": "Welcome", "body": "Your code is 4821."}},
                "ai_instructions": "Warm and brief.",
                "ai_attributes": ["first_name"],
            },
        )
        await create_recipient(
            client,
            addresses=[{"channel": "push", "value": "tok"}],
            attributes={"first_name": "Ann", "income": 1000},
        )
        yield client


async def _send(client: httpx.AsyncClient, key: str) -> None:
    response = await client.post(
        "/v1/messages",
        json={
            "recipient": "user-1",
            "template": "welcome",
            "channels": ["push"],
            "idempotency_key": key,
        },
    )
    assert response.status_code == 202, response.text


async def test_copy_is_personalised_and_usage_recorded(
    ai_client: httpx.AsyncClient, ai_runtime: Runtime, push: FakeChannel, generator: FakeGenerator
) -> None:
    await _send(ai_client, "1")
    await drain(ai_runtime)
    assert push.sent[0].content.body == "Ann, Your code is 4821."
    # Only allow-listed attributes reach the model.
    assert dict(generator.requests[0].context) == {"first_name": "Ann"}
    assert generator.requests[0].instructions == "Warm and brief."
    message = (await ai_client.get("/v1/messages")).json()["items"][0]
    assert message["generated_by"] == "ai:fake-model"


async def test_budget_exhaustion_falls_back_to_template(
    ai_client: httpx.AsyncClient, ai_runtime: Runtime, push: FakeChannel, generator: FakeGenerator
) -> None:
    for key in ("1", "2", "3"):
        await _send(ai_client, key)
        await drain(ai_runtime)
    bodies = [d.content.body for d in push.sent]
    # 120 tokens per call against a budget of 200: two calls, then template copy.
    assert bodies == ["Ann, Your code is 4821.", "Ann, Your code is 4821.", "Your code is 4821."]
    assert len(generator.requests) == 2


async def test_generation_errors_fall_back_to_template(
    ai_client: httpx.AsyncClient, ai_runtime: Runtime, push: FakeChannel, generator: FakeGenerator
) -> None:
    generator.fail = True
    await _send(ai_client, "1")
    await drain(ai_runtime)
    assert push.sent[0].content.body == "Your code is 4821."
    message = (await ai_client.get("/v1/messages")).json()["items"][0]
    assert message["status"] == "sent"
    assert message["generated_by"] == "template"
