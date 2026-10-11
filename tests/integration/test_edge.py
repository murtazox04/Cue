from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from cue_notify.api.app import create_app
from cue_notify.core.enums import Scope
from cue_notify.core.time import utcnow
from cue_notify.db.models import ApiKey
from cue_notify.runtime import Runtime
from cue_notify.services import api_keys
from cue_notify.settings import FailedAuthRateLimit, PageRateLimit, RateLimitSettings

TWO_MEGABYTES = 2_000_000


def connect(
    runtime: Runtime,
    key: str | None = None,
    *,
    address: str = "127.0.0.1",
) -> httpx.AsyncClient:
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(address, 123)),
        base_url="http://cue.test",
        headers=headers,
    )


async def streamed_body() -> AsyncIterator[bytes]:
    for _ in range(32):
        yield b"x" * 65_536


async def post_broadcast(http: httpx.AsyncClient, key: str | None, *, stream: bool) -> int:
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    content = streamed_body() if stream else b"x" * TWO_MEGABYTES
    response = await http.post("/v1/broadcasts", content=content, headers=headers)
    return response.status_code


async def test_only_admin_keys_get_the_broadcast_body_limit(
    runtime: Runtime, admin_key: str
) -> None:
    async with runtime.transaction() as session:
        _, writer_key = await api_keys.create(session, "producer", [Scope.EVENTS_WRITE])
    async with connect(runtime) as http:
        for key in (None, writer_key, "ck_000000000000_forged", "not-a-key"):
            for stream in (False, True):
                assert await post_broadcast(http, key, stream=stream) == 413
        for stream in (False, True):
            assert await post_broadcast(http, admin_key, stream=stream) == 422


async def test_unauthenticated_routes_are_rate_limited(runtime: Runtime) -> None:
    runtime.settings.api.rate_limit = RateLimitSettings(pages=PageRateLimit(per_minute=1, burst=2))
    async with connect(runtime) as http:
        for _ in range(2):
            assert (await http.get("/preferences/unknown-token")).status_code == 404
        refused = await http.get("/review/unknown-token")
        assert refused.status_code == 429
        assert refused.headers["content-type"] == "application/problem+json"
        assert refused.headers["retry-after"] == "60"
        assert refused.json()["status"] == 429
        assert refused.json()["title"] == "Too many requests"
        tracked = await http.post("/v1/track/unknown-token", json={"type": "opened"})
        assert tracked.status_code != 429


async def test_failed_authentication_is_limited_per_address(
    runtime: Runtime, admin_key: str
) -> None:
    runtime.settings.api.rate_limit = RateLimitSettings(
        failed_auth=FailedAuthRateLimit(per_minute=1, burst=3)
    )
    async with runtime.transaction() as session:
        _, reader_key = await api_keys.create(session, "reader", [Scope.MESSAGES_READ])
        _, revoked_key = await api_keys.create(session, "old", [Scope.MESSAGES_READ])
    async with runtime.transaction() as session:
        await session.execute(
            update(ApiKey).where(ApiKey.name == "old").values(revoked_at=utcnow())
        )

    def bearer(key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    async with connect(runtime, "ck_000000000000_guess") as guesser:
        for _ in range(3):
            assert (await guesser.get("/v1/messages")).status_code == 401
        blocked = await guesser.get("/v1/messages")
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) >= 1
        for key in (admin_key, reader_key):
            for _ in range(3):
                valid = await guesser.get("/v1/messages", headers=bearer(key))
                assert valid.status_code == 200
        assert await post_broadcast(guesser, admin_key, stream=False) == 422
        assert (await guesser.get("/v1/messages")).status_code == 429
        assert (await guesser.get("/v1/messages", headers=bearer(revoked_key))).status_code == 429
        anonymous = await guesser.get("/v1/messages", headers={"Authorization": ""})
        assert anonymous.status_code == 429
    async with connect(runtime, admin_key, address="10.1.2.3") as other:
        for _ in range(5):
            assert (await other.get("/v1/messages")).status_code == 200


async def test_the_middleware_key_lookup_is_shared(
    runtime: Runtime, admin_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.settings.api.rate_limit = RateLimitSettings(
        failed_auth=FailedAuthRateLimit(per_minute=1, burst=1)
    )
    lookups: list[str] = []
    authenticate = api_keys.authenticate

    async def counting(session: AsyncSession, raw: str) -> ApiKey | None:
        lookups.append(raw)
        return await authenticate(session, raw)

    async with connect(runtime, "ck_000000000000_guess") as http:
        assert (await http.get("/v1/messages")).status_code == 401
        monkeypatch.setattr(api_keys, "authenticate", counting)
        assert await post_broadcast(http, admin_key, stream=False) == 422
    assert lookups == [admin_key]


@pytest.mark.parametrize(
    ("trusted", "second_status"), [([], 429), (["127.0.0.0/8"], 404)], ids=["ignored", "trusted"]
)
async def test_forwarded_for_is_believed_only_from_trusted_proxies(
    runtime: Runtime, trusted: list[str], second_status: int
) -> None:
    runtime.settings.api.rate_limit = RateLimitSettings(pages=PageRateLimit(per_minute=1, burst=1))
    runtime.settings.api.trusted_proxies = trusted
    async with connect(runtime) as http:
        first = await http.get("/preferences/x", headers={"X-Forwarded-For": "203.0.113.1"})
        assert first.status_code == 404
        second = await http.get("/preferences/x", headers={"X-Forwarded-For": "203.0.113.2"})
        assert second.status_code == second_status
        spoofed = await http.get(
            "/preferences/x", headers={"X-Forwarded-For": "198.51.100.9, 203.0.113.1"}
        )
        assert spoofed.status_code == 429


async def test_rate_limits_can_be_turned_off(runtime: Runtime) -> None:
    runtime.settings.api.rate_limit = RateLimitSettings(
        enabled=False,
        pages=PageRateLimit(per_minute=1, burst=1),
        failed_auth=FailedAuthRateLimit(per_minute=1, burst=1),
    )
    async with connect(runtime, "ck_000000000000_guess") as http:
        for _ in range(5):
            assert (await http.get("/preferences/x")).status_code == 404
            assert (await http.get("/v1/messages")).status_code == 401
