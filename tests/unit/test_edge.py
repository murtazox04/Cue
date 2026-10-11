from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from starlette.types import Receive, Scope, Send

from cue_notify.api.limits import app_path
from cue_notify.api.ratelimit import TokenBuckets
from cue_notify.channels.webhook import WebhookChannel, WebhookConfig
from cue_notify.config_warnings import configuration_warnings
from cue_notify.mcp_http import RequireBearer, create_app
from cue_notify.settings import ApiSettings, Settings, TokenBucketSettings, is_loopback_host
from tests.helpers import TEST_SECRET_KEY

TOKEN = "t" * 40


def test_one_rate_limit_value_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUE_API__RATE_LIMIT__FAILED_AUTH__BURST", "2")
    limits = Settings().api.rate_limit
    assert (limits.failed_auth.per_minute, limits.failed_auth.burst) == (10, 2)
    assert (limits.pages.per_minute, limits.pages.burst) == (60, 30)
    assert limits.enabled


def test_trusted_proxies_are_networks() -> None:
    api = ApiSettings(trusted_proxies=["10.1.2.3/8", "2001:db8::1", " 192.168.0.1 "])
    assert api.trusted_proxies == ["10.0.0.0/8", "2001:db8::1/128", "192.168.0.1/32"]
    with pytest.raises(ValidationError):
        ApiSettings(trusted_proxies=["proxy.internal"])


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_token_buckets_refill_at_the_configured_rate() -> None:
    clock = FakeClock()
    buckets = TokenBuckets(
        TokenBucketSettings(per_minute=30, burst=2), max_clients=1000, clock=clock
    )
    assert buckets.take("a") == 0
    assert buckets.take("a") == 0
    assert buckets.take("a") == 2
    assert buckets.take("b") == 0
    clock.now += 2
    assert buckets.take("a") == 0
    assert buckets.take("a") == 2
    clock.now += 600
    assert [buckets.take("a") for _ in range(3)] == [0, 0, 2]


def test_token_buckets_forget_the_least_recent_clients() -> None:
    buckets = TokenBuckets(
        TokenBucketSettings(per_minute=1, burst=1), max_clients=1000, clock=FakeClock()
    )
    for n in range(1500):
        buckets.take(f"10.0.{n // 256}.{n % 256}")
    assert len(buckets.buckets) == 1000
    assert "10.0.0.0" not in buckets.buckets
    assert buckets.take("10.0.5.219") == 60


@pytest.mark.parametrize(
    ("path", "root_path", "expected"),
    [
        ("/v1/broadcasts", "", "/v1/broadcasts"),
        ("/cue/v1/broadcasts", "/cue", "/v1/broadcasts"),
        ("/v1/broadcasts", "/cue", "/v1/broadcasts"),
        ("/cue", "/cue", "/"),
        ("/cuest/v1", "/cue", "/cuest/v1"),
    ],
)
def test_paths_are_matched_without_the_root_path(path: str, root_path: str, expected: str) -> None:
    assert app_path({"type": "http", "path": path, "root_path": root_path}) == expected


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.1.2.3", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("app.localhost", True),
        ("0.0.0.0", False),
        ("::", False),
        ("10.0.0.5", False),
        ("cue.example.com", False),
    ],
)
def test_loopback_hosts(host: str, loopback: bool) -> None:
    assert is_loopback_host(host) is loopback


@pytest.mark.parametrize(
    ("public_url", "warns"),
    [
        ("http://notify.example.com", True),
        ("http://10.0.0.5:8000", True),
        ("https://notify.example.com", False),
        ("http://localhost:8000", False),
        ("http://127.0.0.1:8000", False),
        (None, False),
    ],
)
def test_plain_http_public_urls_are_reported(public_url: str | None, warns: bool) -> None:
    secret_key = None if public_url is None else TEST_SECRET_KEY
    settings = Settings(api=ApiSettings(public_url=public_url, secret_key=secret_key))
    assert bool(configuration_warnings(settings, {})) is warns


@pytest.mark.parametrize(
    ("options", "warns"),
    [
        ({"url": "https://hooks.example.com/cue"}, True),
        ({"url": "https://hooks.example.com/cue", "secret": "s3cret"}, False),
        ({"url": "https://hooks.example.com/cue", "allow_unsigned": True}, False),
        ({}, False),
    ],
)
async def test_unsigned_fixed_webhooks_are_reported(options: dict[str, Any], warns: bool) -> None:
    async with httpx.AsyncClient() as http:
        channel = WebhookChannel("hook", WebhookConfig.model_validate(options), http)
        warnings = configuration_warnings(Settings(), {"hook": channel})
    assert bool(warnings) is warns
    if warns:
        assert "'hook'" in warnings[0]
        assert "allow_unsigned" in warnings[0]


async def ok(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.parametrize(
    ("authorization", "status"),
    [
        (None, 401),
        ("Bearer wrong", 401),
        (f"Basic {TOKEN}", 401),
        (f"Bearer {TOKEN}x", 401),
        (f"Bearer {TOKEN}", 200),
        (f"bearer {TOKEN}", 200),
    ],
)
async def test_mcp_http_requires_the_bearer_token(authorization: str | None, status: int) -> None:
    headers = {"Authorization": authorization} if authorization else {}
    transport = httpx.ASGITransport(app=RequireBearer(ok, TOKEN))
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as http:
        response = await http.post("/mcp", headers=headers)
    assert response.status_code == status
    if status == 401:
        assert response.headers["www-authenticate"].startswith("Bearer")


async def test_mcp_http_app_is_wrapped_only_with_a_token() -> None:
    guarded = create_app("http://127.0.0.1:8000", "ck_x_y", host="0.0.0.0", token=TOKEN)
    assert isinstance(guarded, RequireBearer)
    open_app = create_app("http://127.0.0.1:8000", "ck_x_y", host="127.0.0.1", token=None)
    assert not isinstance(open_app, RequireBearer)
    transport = httpx.ASGITransport(app=guarded)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as http:
        response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401
