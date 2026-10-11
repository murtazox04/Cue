"""Regression tests from the security review before the 0.3.1 release."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx
import pytest

from cue_notify.api.app import create_app
from cue_notify.observability import RedactTokens
from cue_notify.runtime import Runtime
from tests.helpers import create_recipient


def _connect(runtime: Runtime, key: str | None = None) -> httpx.AsyncClient:
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cue.test", headers=headers
    )


@pytest.fixture
async def agent_key(client: httpx.AsyncClient) -> str:
    """A key for the support agent holding every scope an agent key may have."""
    body = {"key": "support-bot", "name": "Support", "reviewers": []}
    assert (await client.post("/v1/agents", json=body)).status_code == 201
    scopes = ["events:write", "messages:write", "messages:read"]
    response = await client.post(
        "/v1/api-keys", json={"name": "bot-key", "scopes": scopes, "agent": "support-bot"}
    )
    assert response.status_code == 201, response.text
    return response.json()["key"]


@pytest.fixture
async def as_agent(runtime: Runtime, agent_key: str) -> AsyncIterator[httpx.AsyncClient]:
    async with _connect(runtime, agent_key) as http:
        yield http


async def test_agents_cannot_change_where_or_how_people_are_reached(
    client: httpx.AsyncClient, as_agent: httpx.AsyncClient
) -> None:
    await create_recipient(client, "victim", attributes={"vip": True})
    attempts = [
        as_agent.put("/v1/recipients/victim", json={"attributes": {"vip": False}}),
        as_agent.post(
            "/v1/recipients/victim/addresses", json={"channel": "sms", "value": "+15550666"}
        ),
        as_agent.delete("/v1/recipients/victim/addresses/sms/+15550001"),
    ]
    for attempt in attempts:
        assert (await attempt).status_code == 403
    victim = (await client.get("/v1/recipients/victim")).json()
    assert victim["attributes"] == {"vip": True}
    assert {a["value"] for a in victim["addresses"]} == {"token-1", "+15550001"}


async def test_cue_events_cannot_be_forged(
    client: httpx.AsyncClient, as_agent: httpx.AsyncClient
) -> None:
    await create_recipient(client, "lead")
    forged = [
        {"name": "cue.approval_requested", "recipient": "lead"},
        {"name": "order.shipped", "recipient": "lead", "source": "cue"},
    ]
    for body in forged:
        for http in (client, as_agent):
            assert (await http.post("/v1/events", json=body)).status_code == 403


async def test_agents_cannot_claim_other_producers_idempotency_keys(
    client: httpx.AsyncClient, as_agent: httpx.AsyncClient
) -> None:
    await create_recipient(client, "u1")
    squat = {"name": "order.shipped", "recipient": "u1", "idempotency_key": "order-1"}
    first = await as_agent.post("/v1/events", json={**squat, "source": "backend"})
    assert first.status_code == 202
    real = await client.post("/v1/events", json={**squat, "source": "backend"})
    assert real.status_code == 202
    assert real.json()["duplicate"] is False
    events = (await client.get("/v1/events")).json()["items"]
    assert sorted(e["source"] for e in events) == ["agent:support-bot", "backend"]


async def test_keys_of_a_disabled_agent_stop_working(
    client: httpx.AsyncClient, as_agent: httpx.AsyncClient
) -> None:
    assert (await as_agent.get("/v1/messages")).status_code == 200
    disabled = await client.patch("/v1/agents/support-bot", json={"enabled": False})
    assert disabled.status_code == 200
    assert (await as_agent.get("/v1/messages")).status_code == 403


async def test_oversized_bodies_are_refused_before_anything_reads_them(runtime: Runtime) -> None:
    async with _connect(runtime) as anonymous:
        announced = await anonymous.post(
            "/v1/events",
            content=b"x" * 2_000_000,
            headers={"Content-Type": "application/json"},
        )
        assert announced.status_code == 413

        async def stream() -> AsyncIterator[bytes]:
            for _ in range(40):
                yield b"x" * 65_536

        streamed = await anonymous.put(
            "/v1/preferences/whatever",
            content=stream(),
            headers={"Content-Type": "application/json"},
        )
        assert streamed.status_code == 413
        # Small bodies still reach the API (and its authentication).
        small = await anonymous.post("/v1/events", json={"name": "x", "recipient": "y"})
        assert small.status_code == 401


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            '127.0.0.1:5 - "GET /preferences/AbCdEf0123456789xyz HTTP/1.1" 200',
            '127.0.0.1:5 - "GET /preferences/[redacted] HTTP/1.1" 200',
        ),
        ("POST /review/tok_en-0123456789abcdef", "POST /review/[redacted]"),
        ("/v1/track/abcdefgh12345678/open", "/v1/track/[redacted]/open"),
        ("GET /v1/messages", "GET /v1/messages"),
    ],
)
def test_tokens_in_paths_never_reach_the_logs(message: str, expected: str) -> None:
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s", (message,), None)
    record.path = message
    assert RedactTokens().filter(record)
    assert record.getMessage() == expected
    assert record.path == expected


def test_metrics_labels_stay_bounded() -> None:
    pytest.importorskip("prometheus_client")
    from cue_notify.observability import Metrics

    metrics = Metrics(enabled=True)
    for n in range(Metrics.MAX_EVENT_NAMES + 50):
        metrics.event_ingested(f"event.{n}")
    names = {
        sample.labels["name"]
        for family in metrics.registry.collect()
        for sample in family.samples
        if sample.name == "cue_events_total"
    }
    assert len(names) == Metrics.MAX_EVENT_NAMES + 1
    assert "other" in names
