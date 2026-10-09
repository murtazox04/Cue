from __future__ import annotations

from typing import Any

import httpx
import pytest

from cue_notify.runtime import Runtime
from cue_notify.services import broadcasts
from tests.helpers import FakeChannel, create_recipient, create_template, drain

PROMO = {
    "key": "promo",
    "locales": {
        "en": {"title": "Sale", "body": "{{ data.discount }}% off, {{ recipient.external_id }}"}
    },
}


async def _broadcast(client: httpx.AsyncClient, audience: dict[str, Any]) -> dict[str, Any]:
    response = await client.post(
        "/v1/broadcasts",
        json={
            "name": "Autumn sale",
            "template": "promo",
            "category": "marketing",
            "channels": ["push"],
            "audience": audience,
            "variables": {"discount": 20},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def audience(client: httpx.AsyncClient) -> None:
    await create_template(client, **PROMO)
    for index in range(5):
        await create_recipient(
            client,
            f"u{index}",
            addresses=[{"channel": "push", "value": f"tok-{index}"}],
            attributes={"tier": "gold" if index % 2 == 0 else "basic"},
        )


@pytest.mark.usefixtures("audience")
async def test_broadcast_to_everyone(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    created = await _broadcast(client, {"type": "all"})
    assert created["status"] == "draft"
    started = await client.post(f"/v1/broadcasts/{created['id']}/start", json={})
    assert started.json()["status"] == "scheduled"
    await drain(runtime)

    done = (await client.get(f"/v1/broadcasts/{created['id']}")).json()
    assert done["status"] == "completed"
    assert done["stats"]["targeted"] == 5
    assert done["stats"]["queued"] == 5
    assert sorted(d.content.body for d in push.sent) == [f"20% off, u{i}" for i in range(5)]


@pytest.mark.usefixtures("audience")
async def test_filtered_and_listed_audiences(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    gold = await _broadcast(
        client,
        {"type": "filter", "condition": {"==": [{"var": "recipient.attributes.tier"}, "gold"]}},
    )
    await client.post(f"/v1/broadcasts/{gold['id']}/start", json={})
    await drain(runtime)
    assert sorted(d.address for d in push.sent) == ["tok-0", "tok-2", "tok-4"]

    push.sent.clear()
    listed = await _broadcast(client, {"type": "recipients", "external_ids": ["u1", "nobody"]})
    await client.post(f"/v1/broadcasts/{listed['id']}/start", json={})
    await drain(runtime)
    # u1 already got one marketing message today; the cap (2/day) still allows this one.
    assert [d.address for d in push.sent] == ["tok-1"]


@pytest.mark.usefixtures("audience")
async def test_pause_resume_never_duplicates(
    client: httpx.AsyncClient,
    runtime: Runtime,
    push: FakeChannel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broadcasts, "BATCH_SIZE", 2)
    created = await _broadcast(client, {"type": "all"})
    broadcast_id = created["id"]
    await client.post(f"/v1/broadcasts/{broadcast_id}/start", json={})

    # Run exactly one batch, then pause.
    from cue_notify.jobs import create_worker

    worker = create_worker(runtime)
    claimed = await worker._claim(1)
    await worker._execute(claimed[0])
    paused = await client.post(f"/v1/broadcasts/{broadcast_id}/pause")
    assert paused.json()["status"] == "paused"
    await drain(runtime)  # the next batch job sees "paused" and stops
    progress = (await client.get(f"/v1/broadcasts/{broadcast_id}")).json()
    assert progress["stats"]["targeted"] == 2

    resumed = await client.post(f"/v1/broadcasts/{broadcast_id}/resume")
    assert resumed.json()["status"] == "running"
    await drain(runtime)
    done = (await client.get(f"/v1/broadcasts/{broadcast_id}")).json()
    assert done["status"] == "completed"
    assert done["stats"]["targeted"] == 5
    assert len(push.sent) == 5
    assert len({d.address for d in push.sent}) == 5


@pytest.mark.usefixtures("audience")
async def test_cancel_withdraws_queued_messages(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    created = await _broadcast(client, {"type": "all"})
    await client.post(f"/v1/broadcasts/{created['id']}/start", json={})
    worker_runs = await _run_batches_only(runtime)
    assert worker_runs == 1
    cancelled = await client.post(f"/v1/broadcasts/{created['id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    await drain(runtime)
    assert push.sent == []
    statuses = {
        m["status"]
        for m in (await client.get("/v1/messages", params={"broadcast_id": created["id"]})).json()[
            "items"
        ]
    }
    assert statuses == {"cancelled"}
    assert (await client.post(f"/v1/broadcasts/{created['id']}/resume")).status_code == 409


async def _run_batches_only(runtime: Runtime) -> int:
    from cue_notify.jobs import create_worker

    worker = create_worker(runtime)
    runs = 0
    for job in await worker._claim(10):
        if job.kind == broadcasts.BATCH_JOB:
            await worker._execute(job)
            runs += 1
    return runs
