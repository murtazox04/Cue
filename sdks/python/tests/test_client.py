from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from cue_client import AsyncCue, Cue, CueError


def _mock(handler: Any) -> Cue:
    return Cue("ck_test", "http://cue.test", transport=httpx.MockTransport(handler))


def test_problem_details_become_errors() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "title": "Unprocessable content",
                "detail": "request validation failed",
                "errors": [{"loc": ["body", "name"]}],
            },
        )

    with _mock(handler) as cue, pytest.raises(CueError) as raised:
        cue.send_event("x", "u")
    assert (raised.value.status, raised.value.detail) == (422, "request validation failed")
    assert raised.value.errors == [{"loc": ["body", "name"]}]


def test_safe_requests_are_retried_and_unsafe_ones_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("cue_client.client.time.sleep", lambda _: None)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) < 3:
            return httpx.Response(503, headers={"Retry-After": "1"})
        return httpx.Response(202, json={"id": "e1", "status": "pending", "duplicate": False})

    with _mock(handler) as cue:
        assert cue.send_event("x", "u", idempotency_key="k")["id"] == "e1"
    assert len(calls) == 3

    calls.clear()
    with _mock(handler) as cue, pytest.raises(CueError):
        cue.send_event("x", "u")  # no idempotency key: a retry could notify twice
    assert len(calls) == 1


def test_ids_are_escaped_in_paths() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        return httpx.Response(200, json={})

    with _mock(handler) as cue:
        cue.upsert_recipient("../api-keys")
    assert paths == ["/v1/recipients/..%2Fapi-keys"]


# --------------------------------------------------------------------------- real app

server = pytest.mark.skipif(sys.version_info < (3, 12), reason="the server needs Python 3.12")


@pytest.fixture
def database(tmp_path: Path) -> str:
    from alembic import command

    from cue_notify.db.migrations import alembic_config

    url = f"sqlite+aiosqlite:///{tmp_path / 'cue.db'}"
    command.upgrade(alembic_config(url), "head")
    return url


@pytest.fixture
async def cue(database: str) -> AsyncIterator[tuple[AsyncCue, list[Any], Any]]:
    from cue_notify.api.app import create_app
    from cue_notify.channels.console import ConsoleChannel, ConsoleConfig
    from cue_notify.core.enums import Scope
    from cue_notify.jobs import create_worker
    from cue_notify.runtime import Runtime
    from cue_notify.services import api_keys
    from cue_notify.settings import DatabaseSettings, PolicySettings, Settings

    sent: list[Any] = []

    class Recording(ConsoleChannel):
        async def send(self, delivery: Any) -> Any:
            sent.append(delivery)
            return await super().send(delivery)

    settings = Settings(
        environment="test",
        database=DatabaseSettings(url=database),
        policy=PolicySettings(quiet_hours=None),
    )
    channel = Recording("push", ConsoleConfig(), httpx.AsyncClient())
    runtime = Runtime.create(settings, channels={"push": channel})
    async with runtime.transaction() as session:
        _, raw = await api_keys.create(session, "sdk", [Scope.ADMIN])
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    client = AsyncCue(raw, "http://cue.test", transport=httpx.ASGITransport(app=app))

    async def drain() -> None:
        await create_worker(runtime).run_until_idle()

    try:
        yield client, sent, drain
    finally:
        await client.aclose()
        await runtime.aclose()


@server
async def test_round_trip_against_cue(cue: tuple[AsyncCue, list[Any], Any]) -> None:
    client, sent, drain = cue
    await client._request(  # templates are admin configuration, not part of the client
        "POST",
        "/v1/templates",
        json={"key": "hello", "locales": {"en": {"body": "Hello {{ data.name }}"}}},
        safe=False,
    )
    profile = await client.upsert_recipient(
        "user-1", timezone="UTC", addresses=[{"channel": "push", "value": "tok"}]
    )
    assert profile["external_id"] == "user-1"

    message = await client.send_message(
        "user-1", "hello", ["push"], {"name": "Ann"}, importance="high", idempotency_key="k1"
    )
    assert (message["status"], message["importance"]) == ("queued", "high")
    await drain()
    assert sent[0].content.body == "Hello Ann"
    assert (await client.get_message(message["id"]))["status"] == "sent"
    listed = await client.list_messages(recipient="user-1")
    assert [m["id"] for m in listed["items"]] == [message["id"]]

    with pytest.raises(CueError) as raised:
        await client.send_message("user-1", "missing", ["push"])
    assert raised.value.status == 404
