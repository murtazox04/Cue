"""Generic HTTP webhook channel.

Posts a JSON document to ``url`` (or, when ``url`` is not configured, to the address
itself — which makes per-recipient webhooks such as Slack/Discord incoming hooks work).
Requests are signed so receivers can verify they came from Cue::

    Cue-Signature: t=1700000000,v1=<hex HMAC-SHA256 of "{t}.{body}">

Receivers should reject timestamps older than a few minutes to prevent replays.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import ClassVar

import httpx
from pydantic import AnyHttpUrl, Field, SecretStr

from cue_notify.channels.base import (
    Channel,
    ChannelConfig,
    Delivery,
    DeliveryError,
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


def sign(body: bytes, secret: str, timestamp: int) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


class WebhookChannel(Channel[WebhookConfig]):
    provider: ClassVar[str] = "webhook"
    config_model: ClassVar[type[ChannelConfig]] = WebhookConfig

    async def send(self, delivery: Delivery) -> Receipt:
        url = str(self.config.url) if self.config.url else delivery.address
        if not url.startswith(("https://", "http://")):
            raise DeliveryError(f"webhook address is not a URL: {url!r}")
        payload = {
            "id": str(delivery.message_id),
            "channel": self.name,
            "address": delivery.address,
            "locale": delivery.locale,
            "content": delivery.content.model_dump(exclude_none=True),
            "metadata": delivery.metadata,
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
        try:
            response = await self.http.post(
                url, content=body, headers=headers, timeout=self.config.timeout
            )
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc
        raise_for_http(response, invalid_address=response.status_code in (404, 410))
        return Receipt(provider_message_id=response.headers.get("x-request-id"))
