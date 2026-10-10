"""Fatigue: back off from people who stopped paying attention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import update

from cue_notify.db.models import Message
from cue_notify.runtime import Runtime
from tests.helpers import create_recipient, create_template, drain


async def _setup(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/categories", json={"key": "social", "fatigue": {"after": 3, "pause": "P7D"}}
    )
    assert response.status_code == 201, response.text
    assert response.json()["fatigue"] == {"after": 3, "pause": "P7D"}
    await create_template(client)
    await create_recipient(client)


async def _send(
    client: httpx.AsyncClient, runtime: Runtime, importance: str = "normal"
) -> dict[str, Any]:
    response = await client.post(
        "/v1/messages",
        json={
            "recipient": "user-1",
            "template": "order-shipped",
            "category": "social",
            "channels": ["push"],
            "data": {"order_id": "A-1"},
            "importance": importance,
        },
    )
    assert response.status_code == 202, response.text
    await drain(runtime)
    return response.json()


async def test_ignored_messages_pause_the_category(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await _setup(client)
    sent = [await _send(client, runtime) for _ in range(3)]
    assert [m["status"] for m in sent] == ["queued"] * 3

    paused = await _send(client, runtime)
    assert (paused["status"], paused["status_reason"]) == ("suppressed", "fatigue")
    low = await _send(client, runtime, importance="low")
    assert low["status_reason"] == "fatigue"
    # What matters still gets through.
    assert (await _send(client, runtime, importance="high"))["status"] == "queued"


async def test_engagement_ends_the_pause(client: httpx.AsyncClient, runtime: Runtime) -> None:
    await _setup(client)
    sent = [await _send(client, runtime) for _ in range(3)]
    assert (await _send(client, runtime))["status"] == "suppressed"
    response = await client.post(
        f"/v1/messages/{sent[1]['id']}/engagement", json={"type": "opened"}
    )
    assert response.status_code == 200, response.text
    assert (await _send(client, runtime))["status"] == "queued"


async def test_one_message_probes_after_the_pause(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await _setup(client)
    for _ in range(3):
        await _send(client, runtime)
    async with runtime.transaction() as session:
        await session.execute(update(Message).values(send_at=datetime.now(UTC) - timedelta(days=8)))
    probe = await _send(client, runtime)
    assert probe["status"] == "queued"
    # Ignored again: the pause starts over.
    assert (await _send(client, runtime))["status_reason"] == "fatigue"


async def test_fatigue_settings_are_validated(client: httpx.AsyncClient) -> None:
    for fatigue in ({"after": 1, "pause": "P1D"}, {"after": 3, "pause": "PT10M"}):
        response = await client.post("/v1/categories", json={"key": "x", "fatigue": fatigue})
        assert response.status_code == 422, fatigue
