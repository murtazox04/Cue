"""Apple Push Notification service, directly over HTTP/2. The address is a device token.

Authenticates with a token-signing key (``.p8``) from the Apple Developer account, so no
certificates expire. Requires the ``apns`` extra (``pip install 'cue-notify[apns]'``) for
HTTP/2 and ES256 signing.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, ClassVar, Literal, Self

import httpx
from pydantic import Field, FilePath, SecretStr, model_validator

from cue_notify.channels.base import (
    Channel,
    ChannelConfig,
    Delivery,
    DeliveryError,
    InvalidAddressError,
    Receipt,
    finite_delay,
    flatten_data,
    transport_error,
)
from cue_notify.channels.fcm import INTERRUPTION_LEVELS
from cue_notify.core.enums import Importance

HOSTS = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}
DEVICE_TOKEN = re.compile(r"[0-9a-fA-F]{32,200}")
TOKEN_LIFETIME = 50 * 60  # Apple accepts a token for an hour; refresh before that
MAX_PAYLOAD = 4096
# https://developer.apple.com/documentation/usernotifications/handling-notification-responses-from-apns
DEAD_TOKEN_REASONS = frozenset({"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"})
STALE_AUTH_REASONS = frozenset({"ExpiredProviderToken", "InvalidProviderToken"})


class ApnsConfig(ChannelConfig):
    team_id: str = Field(min_length=10, max_length=10)
    key_id: str = Field(min_length=10, max_length=10)
    key_file: FilePath | None = Field(default=None, description="Path to the AuthKey_….p8 file.")
    key: SecretStr | None = Field(default=None, description="The .p8 key inline (PEM).")
    bundle_id: str = Field(description="The app's bundle id, sent as apns-topic.")
    environment: Literal["production", "sandbox"] = "production"
    sound: str | None = "default"
    category: str | None = Field(
        default=None, description="Notification category registered by the app (for actions)."
    )
    ttl_seconds: int | None = Field(default=None, ge=0, description="Default expiry.")

    @model_validator(mode="after")
    def _one_key(self) -> Self:
        if (self.key_file is None) == (self.key is None):
            raise ValueError("set exactly one of key_file or key")
        return self

    def signing_key(self) -> str:
        if self.key_file is not None:
            return Path(self.key_file).read_text()
        return self.key.get_secret_value()  # type: ignore[union-attr]


def payload(delivery: Delivery, config: ApnsConfig) -> dict[str, Any]:
    content = delivery.content
    alert: dict[str, str] = {"body": content.body}
    if content.title:
        alert["title"] = content.title
    aps: dict[str, Any] = {
        "alert": alert,
        "interruption-level": INTERRUPTION_LEVELS[delivery.importance],
    }
    if config.sound and delivery.importance is not Importance.LOW:
        aps["sound"] = config.sound
    if content.thread:
        aps["thread-id"] = content.thread
    if config.category:
        aps["category"] = config.category
    if content.image_url:
        aps["mutable-content"] = 1  # lets a notification service extension fetch it
    body: dict[str, Any] = {"aps": aps, **flatten_data(content.data), **delivery.metadata}
    if content.url:
        body["url"] = content.url
    if content.image_url:
        body["image_url"] = content.image_url
    if content.actions:
        body["cue_actions"] = [
            {"id": a.id, "label": a.label, **({"url": a.url} if a.url else {})}
            for a in content.actions
        ]
    return body


class _ProviderToken:
    """An ES256 JWT, reused for most of its hour and signed again when Apple rejects it."""

    def __init__(self, config: ApnsConfig) -> None:
        self._config = config
        self._token: str | None = None
        self._issued_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._token = None

    async def get(self) -> str:
        async with self._lock:
            now = time.time()
            if self._token and now - self._issued_at < TOKEN_LIFETIME:
                return self._token
            try:
                import jwt
            except ImportError as exc:  # pragma: no cover
                raise DeliveryError("APNs requires: pip install 'cue-notify[apns]'") from exc
            self._token = jwt.encode(
                {"iss": self._config.team_id, "iat": int(now)},
                self._config.signing_key(),
                algorithm="ES256",
                headers={"kid": self._config.key_id},
            )
            self._issued_at = now
            return self._token


class ApnsChannel(Channel[ApnsConfig]):
    provider: ClassVar[str] = "apns"
    config_model: ClassVar[type[ChannelConfig]] = ApnsConfig

    def __init__(self, name: str, config: ApnsConfig, http: httpx.AsyncClient) -> None:
        super().__init__(name, config, http)
        self._token = _ProviderToken(config)
        self._client = self.make_client()

    def make_client(self) -> httpx.AsyncClient:
        """APNs speaks only HTTP/2, so the channel keeps its own client."""
        try:
            import h2  # noqa: F401  # pyright: ignore[reportUnusedImport]
        except ImportError as exc:
            raise ValueError("APNs requires: pip install 'cue-notify[apns]'") from exc
        return httpx.AsyncClient(http2=True, timeout=httpx.Timeout(15.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    def headers(self, delivery: Delivery) -> dict[str, str]:
        headers = {
            "apns-topic": self.config.bundle_id,
            "apns-push-type": "alert",
            "apns-priority": "5" if delivery.importance is Importance.LOW else "10",
            "apns-id": str(delivery.message_id),
        }
        if delivery.expires_at is not None:
            headers["apns-expiration"] = str(max(int(delivery.expires_at.timestamp()), 0))
        elif self.config.ttl_seconds is not None:
            headers["apns-expiration"] = str(int(time.time()) + self.config.ttl_seconds)
        return headers

    async def send(self, delivery: Delivery) -> Receipt:
        body = json.dumps(
            payload(delivery, self.config), ensure_ascii=False, separators=(",", ":")
        ).encode()
        if len(body) > MAX_PAYLOAD:
            raise DeliveryError(f"APNs payload is {len(body)} bytes; the limit is {MAX_PAYLOAD}")
        if not DEVICE_TOKEN.fullmatch(delivery.address):
            raise InvalidAddressError("not an APNs device token (hexadecimal)")
        url = f"{HOSTS[self.config.environment]}/3/device/{delivery.address}"
        for attempt in range(2):
            headers = self.headers(delivery)
            headers["authorization"] = f"bearer {await self._token.get()}"
            try:
                response = await self._client.post(url, content=body, headers=headers)
            except httpx.HTTPError as exc:
                raise transport_error(exc) from exc
            if response.is_success:
                return Receipt(provider_message_id=response.headers.get("apns-id"))
            reason = _reason(response)
            if reason in STALE_AUTH_REASONS and attempt == 0:
                self._token.invalidate()  # sign a fresh token and try once more
                continue
            detail = f"apns {response.status_code}: {reason}"
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = finite_delay(response.headers.get("retry-after"))
                raise DeliveryError(detail, retryable=True, retry_after=retry_after)
            if response.status_code == 410 or reason in DEAD_TOKEN_REASONS:
                raise InvalidAddressError(detail)
            raise DeliveryError(detail)
        raise DeliveryError("apns: provider token rejected")  # pragma: no cover


def _reason(response: httpx.Response) -> str:
    try:
        return str(response.json().get("reason", "")) or f"HTTP {response.status_code}"
    except ValueError:
        return f"HTTP {response.status_code}"
