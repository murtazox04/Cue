"""Generic HTTP webhook channel.

Posts a JSON document to ``url``, or — when ``url`` is not configured — to the address
itself, which makes per-recipient webhooks (Slack, Discord or Teams incoming hooks) work.

Per-recipient URLs come from API callers, so they are treated as untrusted: they must be
``https``, must resolve only to public IP addresses, and the request is pinned to the
address that was checked (no DNS-rebinding window). Response bodies from such URLs are
never stored. A fixed ``url`` is operator configuration and is trusted.

Requests are signed so receivers can verify they came from Cue::

    Cue-Signature: t=1700000000,v1=<hex HMAC-SHA256 of "{t}.{body}">

Receivers should reject timestamps older than a few minutes to prevent replays.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
import time
from typing import Any, ClassVar

import httpx
from pydantic import AnyHttpUrl, Field, SecretStr

from cue_notify.channels.base import (
    Channel,
    ChannelConfig,
    Delivery,
    DeliveryError,
    InvalidAddressError,
    Receipt,
    raise_for_http,
    transport_error,
)

SIGNATURE_HEADER = "Cue-Signature"


class WebhookConfig(ChannelConfig):
    url: AnyHttpUrl | None = Field(default=None, description="Fixed endpoint; else the address.")
    secret: SecretStr | None = Field(default=None, description="HMAC signing secret.")
    headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = Field(default=10.0, gt=0)
    allow_private_networks: bool = Field(
        default=False,
        description="Let per-recipient URLs reach private, loopback or link-local addresses.",
    )
    allow_http: bool = Field(default=False, description="Allow plain-http per-recipient URLs.")


def sign(body: bytes, secret: str, timestamp: int) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def is_public(ip: str) -> bool:
    address = ipaddress.ip_address(ip)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


class WebhookChannel(Channel[WebhookConfig]):
    provider: ClassVar[str] = "webhook"
    config_model: ClassVar[type[ChannelConfig]] = WebhookConfig

    async def resolve(self, host: str, port: int) -> list[str]:
        """Resolve ``host`` to IP addresses (overridable for tests)."""
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return [str(info[4][0]) for info in infos]

    async def _pinned_target(
        self, address: str
    ) -> tuple[httpx.URL, dict[str, str], dict[str, Any]]:
        """Validate an untrusted URL and return (url pinned to an IP, headers, extensions)."""
        try:
            url = httpx.URL(address)
        except httpx.InvalidURL as exc:
            raise InvalidAddressError(f"invalid webhook URL: {exc}") from exc
        allowed = ("https", "http") if self.config.allow_http else ("https",)
        if url.scheme not in allowed or not url.host:
            raise InvalidAddressError(f"webhook URL must be {' or '.join(allowed)}")
        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            ips = [url.host] if _is_ip(url.host) else await self.resolve(url.host, port)
        except OSError as exc:
            raise DeliveryError(f"cannot resolve {url.host}: {exc}", retryable=True) from exc
        if not ips:
            raise DeliveryError(f"cannot resolve {url.host}", retryable=True)
        if not self.config.allow_private_networks and not all(is_public(ip) for ip in ips):
            raise InvalidAddressError("webhook URL resolves to a non-public address")
        host_header = url.host if url.port is None else f"{url.host}:{url.port}"
        extensions: dict[str, Any] = {}
        if url.scheme == "https" and not _is_ip(url.host):
            extensions["sni_hostname"] = url.host  # TLS still verifies the real hostname
        return url.copy_with(host=ips[0]), {"Host": host_header}, extensions

    async def send(self, delivery: Delivery) -> Receipt:
        payload = {
            "id": str(delivery.message_id),
            "channel": self.name,
            "address": delivery.address,
            "locale": delivery.locale,
            "content": delivery.content.model_dump(exclude_none=True),
            "metadata": delivery.metadata,
            "links": delivery.links,
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": str(delivery.message_id),
            **self.config.headers,
        }
        if self.config.secret:
            headers[SIGNATURE_HEADER] = sign(
                body, self.config.secret.get_secret_value(), int(time.time())
            )

        trusted = self.config.url is not None
        extensions: dict[str, Any] = {}
        if trusted:
            url = httpx.URL(str(self.config.url))
        else:
            url, pinned_headers, extensions = await self._pinned_target(delivery.address)
            headers |= pinned_headers
        try:
            response = await self.http.post(
                url,
                content=body,
                headers=headers,
                timeout=self.config.timeout,
                extensions=extensions,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc
        raise_for_http(
            response, invalid_address=response.status_code in (404, 410), include_body=trusted
        )
        return Receipt(provider_message_id=response.headers.get("x-request-id"))


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
