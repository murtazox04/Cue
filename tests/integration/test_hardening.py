"""Regression tests for races, abuse and failure handling."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, update

from cue_notify import queue
from cue_notify.core.enums import EventStatus, MessageStatus, Scope
from cue_notify.db.models import Event, Job, Message
from cue_notify.jobs import create_worker
from cue_notify.runtime import Runtime
from cue_notify.services import api_keys
from tests.helpers import FakeChannel, create_recipient, create_rule, create_template, drain


async def _key(runtime: Runtime, name: str, *scopes: Scope) -> dict[str, str]:
    async with runtime.transaction() as session:
        _, raw = await api_keys.create(session, name, scopes)
    return {"Authorization": f"Bearer {raw}"}


def _send(**overrides: object) -> dict[str, object]:
    return {
        "recipient": "user-1",
        "template": "order-shipped",
        "channels": ["push"],
        "data": {"order_id": "1"},
        **overrides,
    }


async def test_claimed_message_cannot_be_cancelled(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await create_template(client)
    await create_recipient(client)
    message = (await client.post("/v1/messages", json=_send())).json()
    async with runtime.transaction() as session:  # a worker has claimed it
        await session.execute(
            update(Message)
            .where(Message.id == UUID(message["id"]))
            .values(status=MessageStatus.SENDING)
        )
    response = await client.post(f"/v1/messages/{message['id']}/cancel")
    assert response.status_code == 409
    assert (await client.get(f"/v1/messages/{message['id']}")).json()["status"] == "sending"


async def test_cancelled_message_is_never_sent(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_template(client)
    await create_recipient(client)
    message = (await client.post("/v1/messages", json=_send())).json()
    assert (await client.post(f"/v1/messages/{message['id']}/cancel")).status_code == 200
    await drain(runtime)
    assert push.sent == []


async def test_hostile_event_data_does_not_wedge_processing(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_template(client)
    await create_recipient(client)
    await create_rule(
        client,
        key="math",
        priority=1,
        stop_processing=False,
        condition={">": [{"+": [{"var": "event.data.x"}, {"var": "event.data.x"}]}, 1]},
    )
    await create_rule(client, key="fallback")
    response = await client.post(
        "/v1/events",
        json={
            "name": "order.shipped",
            "recipient": "user-1",
            "data": {"x": 1e308, "order_id": "1"},
        },
    )
    await drain(runtime)
    event = (await client.get(f"/v1/events/{response.json()['id']}")).json()
    assert event["status"] == "processed"
    assert event["outcome"][0]["reason"].startswith("condition_error")
    assert len(push.sent) == 1  # the other rule still ran


async def test_dead_jobs_mark_their_rows_failed(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    runtime.settings.worker.max_attempts = 1
    await create_template(client)
    await create_recipient(client)

    async def explode(_: queue.JobContext) -> None:
        raise RuntimeError("database on fire")

    from cue_notify.jobs import HANDLERS, ON_DEAD

    event = (await client.post("/v1/events", json={"name": "x", "recipient": "user-1"})).json()
    message = (await client.post("/v1/messages", json=_send())).json()
    worker = queue.Worker(runtime, dict.fromkeys(HANDLERS, explode), on_dead=ON_DEAD)
    await worker.run_until_idle()

    async with runtime.session() as session:
        stored_event = await session.get(Event, UUID(event["id"]))
        stored_message = await session.get(Message, UUID(message["id"]))
        dead = (await session.scalars(select(Job).where(Job.dead.is_(True)))).all()
    assert stored_event is not None and stored_event.status is EventStatus.FAILED
    assert stored_event.error == "RuntimeError: database on fire"
    assert stored_message is not None and stored_message.status is MessageStatus.FAILED
    assert {job.key for job in dead} == {None}  # keys released so work can be re-enqueued


async def test_inline_addresses_need_recipients_scope(
    client: httpx.AsyncClient, runtime: Runtime
) -> None:
    await create_template(client)
    headers = await _key(runtime, "producer", Scope.EVENTS_WRITE, Scope.MESSAGES_WRITE)
    attacker = {"external_id": "victim", "addresses": [{"channel": "push", "value": "evil"}]}
    event = await client.post(
        "/v1/events", headers=headers, json={"name": "x", "recipient": attacker}
    )
    assert event.status_code == 403
    send = await client.post("/v1/messages", headers=headers, json=_send(recipient=attacker))
    assert send.status_code == 403
    profile_only = await client.post(
        "/v1/events",
        headers=headers,
        json={"name": "x", "recipient": {"external_id": "victim", "locale": "uz"}},
    )
    assert profile_only.status_code == 202


async def test_concurrent_profile_updates_do_not_conflict_or_lose_data(
    client: httpx.AsyncClient,
) -> None:
    await create_recipient(client, "busy", addresses=[])

    async def ingest(index: int) -> int:
        recipient = {
            "external_id": "busy",
            "attributes": {f"a{index}": index},
            "addresses": [{"channel": "push", "value": "same-device"}],
        }
        response = await client.post("/v1/events", json={"name": "open", "recipient": recipient})
        return response.status_code

    statuses = await asyncio.gather(*(ingest(i) for i in range(8)))
    assert statuses == [202] * 8
    profile = (await client.get("/v1/recipients/busy")).json()
    assert set(profile["attributes"]) == {f"a{i}" for i in range(8)}
    assert [a["value"] for a in profile["addresses"]] == ["same-device"]


async def test_restarting_a_scheduled_broadcast_moves_it(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_template(client, key="promo", locales={"en": {"body": "Hi"}})
    await create_recipient(client)
    broadcast = (
        await client.post(
            "/v1/broadcasts",
            json={
                "name": "b",
                "template": "promo",
                "category": "transactional",
                "channels": ["push"],
                "audience": {"type": "all"},
            },
        )
    ).json()
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    await client.post(f"/v1/broadcasts/{broadcast['id']}/start", json={"at": tomorrow})
    assert await drain(runtime) == 0
    await client.post(f"/v1/broadcasts/{broadcast['id']}/start", json={})
    await drain(runtime)
    assert len(push.sent) == 1


async def test_invalid_broadcast_filter_is_rejected(client: httpx.AsyncClient) -> None:
    await create_template(client)
    response = await client.post(
        "/v1/broadcasts",
        json={
            "name": "b",
            "template": "order-shipped",
            "channels": ["push"],
            "audience": {"type": "filter", "condition": {"bogus_op": [1]}},
        },
    )
    assert response.status_code == 422


async def test_engagement_cannot_be_rewritten(
    client: httpx.AsyncClient, runtime: Runtime, push: FakeChannel
) -> None:
    await create_template(client)
    await create_recipient(client)
    message = (await client.post("/v1/messages", json=_send())).json()
    await drain(runtime)
    url = f"/v1/messages/{message['id']}/engagement"
    await client.post(
        url, json={"type": "converted", "value": 10, "occurred_at": "1970-01-01T00:00:00Z"}
    )
    await client.post(url, json={"type": "converted", "value": 999999})
    weird = await client.post(
        url, json={"type": "opened", "occurred_at": "0001-01-01T00:00:00+05:00"}
    )
    assert weird.status_code == 200
    stored = (await client.get(f"/v1/messages/{message['id']}")).json()
    assert stored["converted_at"] >= stored["sent_at"]  # clamped, not 1970
    async with runtime.session() as session:
        row = await session.get(Message, UUID(message["id"]))
    assert row is not None and row.conversion_value == 10


async def test_naive_datetimes_are_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/api-keys",
        json={"name": "x", "scopes": ["admin"], "expires_at": "2030-01-01T00:00:00"},
    )
    assert response.status_code == 422


async def test_api_key_names_are_unique_and_events_take_a_source(
    client: httpx.AsyncClient,
) -> None:
    body = {"name": "billing", "scopes": ["events:write"]}
    assert (await client.post("/v1/api-keys", json=body)).status_code == 201
    assert (await client.post("/v1/api-keys", json=body)).status_code == 409
    event = {"name": "x", "recipient": "u", "idempotency_key": "k", "source": "billing"}
    first = await client.post("/v1/events", json=event)
    other_source = await client.post("/v1/events", json={**event, "source": "crm"})
    repeat = await client.post("/v1/events", json=event)
    assert (first.status_code, other_source.status_code, repeat.status_code) == (202, 202, 200)


async def test_overrun_lease_cannot_clobber_the_new_owner(runtime: Runtime) -> None:
    async with runtime.transaction() as session:
        await queue.enqueue(session, "slow")
    first = create_worker(runtime, name="a")
    [job_a] = await first._claim(1)
    async with runtime.transaction() as session:  # the lease expires…
        await session.execute(
            update(Job).values(locked_until=datetime.now(UTC) - timedelta(seconds=1))
        )
    second = create_worker(runtime, name="b")
    [job_b] = await second._claim(1)
    assert job_b.locked_by != job_a.locked_by

    first.handlers["slow"] = _noop
    await first._execute(job_a)  # the stale owner finishes late
    async with runtime.session() as session:
        still_there = await session.get(Job, job_b.id)
    assert still_there is not None
    assert still_there.locked_by == job_b.locked_by


async def _noop(_: queue.JobContext) -> None:
    return None


@pytest.mark.usefixtures("runtime")
async def test_worker_survives_database_errors(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = create_worker(runtime)
    calls = 0
    recovered = asyncio.Event()
    real_claim = worker._claim

    async def flaky_claim(limit: int) -> list[Job]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("database restarted")
        if calls >= 3:
            recovered.set()
        return await real_claim(limit)

    monkeypatch.setattr(worker, "_claim", flaky_claim)
    runtime.settings.worker.poll_interval = 0.01
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    await asyncio.wait_for(recovered.wait(), timeout=5)
    stop.set()
    await task  # returned normally: the failure did not kill the loop
