from __future__ import annotations

import ipaddress
import math
import time
from collections.abc import Callable, Sequence

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cue_notify.api.limits import app_path, presented_key
from cue_notify.api.problems import problem
from cue_notify.settings import RateLimitSettings, TokenBucketSettings

type Clock = Callable[[], float]
type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

API_PREFIX = "/v1"
PAGE_PREFIXES = ("/preferences", "/review", "/v1/preferences")
TRACKING_PREFIX = "/v1/track"
INBOUND_PREFIX = "/v1/inbound"
FEEDBACK_PREFIXES = ("/v1/inbound/twilio/status", "/v1/inbound/resend", "/v1/inbound/ses")


def under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


class TokenBuckets:
    def __init__(self, limit: TokenBucketSettings, *, max_clients: int, clock: Clock) -> None:
        self.capacity = float(limit.burst)
        self.rate = limit.per_minute / 60
        self.max_clients = max_clients
        self.clock = clock
        self.buckets: dict[str, tuple[float, float]] = {}

    def level(self, client: str) -> float:
        now = self.clock()
        tokens, updated = self.buckets.pop(client, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - updated) * self.rate)
        self.buckets[client] = (tokens, now)
        while len(self.buckets) > self.max_clients:
            del self.buckets[next(iter(self.buckets))]
        return tokens

    def wait(self, tokens: float) -> int:
        return max(1, math.ceil((1 - tokens) / self.rate))

    def check(self, client: str) -> int:
        tokens = self.level(client)
        return 0 if tokens >= 1 else self.wait(tokens)

    def spend(self, client: str) -> None:
        tokens = self.level(client)
        self.buckets[client] = (max(0.0, tokens - 1), self.buckets[client][1])

    def take(self, client: str) -> int:
        wait = self.check(client)
        if not wait:
            self.spend(client)
        return wait


class RateLimit:
    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: RateLimitSettings,
        trusted_proxies: Sequence[str] = (),
        clock: Clock = time.monotonic,
    ) -> None:
        self.app = app
        self.enabled = settings.enabled

        def buckets(limit: TokenBucketSettings) -> TokenBuckets:
            return TokenBuckets(limit, max_clients=settings.max_clients, clock=clock)

        self.pages = buckets(settings.pages)
        self.tracking = buckets(settings.tracking)
        self.inbound = buckets(settings.inbound)
        self.feedback = buckets(settings.feedback)
        self.failed_auth = buckets(settings.failed_auth)
        self.trusted: list[Network] = [
            ipaddress.ip_network(entry, strict=False) for entry in trusted_proxies
        ]

    def public_buckets(self, path: str) -> TokenBuckets | None:
        if any(under(path, prefix) for prefix in PAGE_PREFIXES):
            return self.pages
        if under(path, TRACKING_PREFIX):
            return self.tracking
        if any(under(path, prefix) for prefix in FEEDBACK_PREFIXES):
            return self.feedback
        if under(path, INBOUND_PREFIX):
            return self.inbound
        return None

    def is_trusted(self, host: str) -> bool:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(address in network for network in self.trusted)

    def client_address(self, scope: Scope) -> str:
        client: tuple[str, int] | None = scope.get("client")
        peer = client[0] if client else ""
        if not self.trusted or not self.is_trusted(peer):
            return peer
        hops = [
            hop.strip()
            for name, value in scope["headers"]
            if name == b"x-forwarded-for"
            for hop in value.decode("latin-1").split(",")
            if hop.strip()
        ]
        for hop in reversed(hops):
            if not self.is_trusted(hop):
                return hop
        return hops[0] if hops else peer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return
        path = app_path(scope)
        client = self.client_address(scope)
        public = self.public_buckets(path)
        if public is not None:
            wait = public.take(client)
            if wait:
                await refuse(scope, receive, send, wait)
                return
        if not under(path, API_PREFIX):
            await self.app(scope, receive, send)
            return
        wait = self.failed_auth.check(client)
        if wait and await presented_key(scope) is None:
            await refuse(scope, receive, send, wait)
            return

        async def counting_send(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] == 401:
                self.failed_auth.spend(client)
            await send(message)

        await self.app(scope, receive, counting_send)


async def refuse(scope: Scope, receive: Receive, send: Send, wait: int) -> None:
    response = problem(
        429,
        "Too many requests",
        f"rate limit exceeded; retry in {wait} s",
        headers={"Retry-After": str(wait)},
    )
    await response(scope, receive, send)
