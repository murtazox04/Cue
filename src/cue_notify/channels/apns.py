from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
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

logger = logging.getLogger(__name__)

HOSTS = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}
DEVICE_TOKEN = re.compile(r"[0-9a-fA-F]{32,200}")
TOKEN_REFRESH_SECONDS = 50 * 60
MAX_PAYLOAD = 4096
DEAD_TOKEN_REASONS = frozenset({"BadDeviceToken", "Unregistered", "ExpiredToken"})
CONFIG_REASONS = frozenset(
    {
        "DeviceTokenNotForTopic",
        "InvalidProviderToken",
        "BadTopic",
        "TopicDisallowed",
        "BadEnvironmentKeyIdInToken",
        "MissingProviderToken",
        "Forbidden",
    }
)
STALE_SIGNATURE_REASON = "ExpiredProviderToken"


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
    def exactly_one_key(self) -> Self:
        if (self.key_file is None) == (self.key is None):
            raise ValueError("set exactly one of key_file or key")
        return self

    def signing_key(self) -> str:
        if self.key_file is not None:
            return Path(self.key_file).read_text()
        if self.key is None:
            raise ValueError("set exactly one of key_file or key")
        return self.key.get_secret_value()


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
        aps["mutable-content"] = 1
    body: dict[str, Any] = {**flatten_data(content.data), **delivery.metadata}
    if content.url:
        body["url"] = content.url
    if content.image_url:
        body["image_url"] = content.image_url
    if content.actions:
        body["cue_actions"] = [
            {"id": a.id, "label": a.label, **({"url": a.url} if a.url else {})}
            for a in content.actions
        ]
    body["aps"] = aps
    return body


class ProviderToken:
    def __init__(self, config: ApnsConfig) -> None:
        self.config = config
        self.token: str | None = None
        self.issued_at = 0.0
        self.lock = asyncio.Lock()

    def invalidate(self, rejected: str) -> None:
        if self.token == rejected:
            self.token = None

    async def get(self) -> str:
        async with self.lock:
            now = time.time()
            if self.token and now - self.issued_at < TOKEN_REFRESH_SECONDS:
                return self.token
            try:
                import jwt
            except ImportError as exc:
                raise DeliveryError("APNs requires: pip install 'cue-notify[apns]'") from exc
            self.token = jwt.encode(
                {"iss": self.config.team_id, "iat": int(now)},
                self.config.signing_key(),
                algorithm="ES256",
                headers={"kid": self.config.key_id},
            )
            self.issued_at = now
            return self.token


class ApnsChannel(Channel[ApnsConfig]):
    provider: ClassVar[str] = "apns"
    config_model: ClassVar[type[ChannelConfig]] = ApnsConfig

    def __init__(self, name: str, config: ApnsConfig, http: httpx.AsyncClient) -> None:
        super().__init__(name, config, http)
        self.provider_token = ProviderToken(config)
        self.client = self.make_client()

    def make_client(self) -> httpx.AsyncClient:
        if importlib.util.find_spec("h2") is None:
            raise ValueError("APNs requires: pip install 'cue-notify[apns]'")
        return httpx.AsyncClient(http2=True, timeout=httpx.Timeout(15.0))

    async def aclose(self) -> None:
        await self.client.aclose()

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

    async def post(self, url: str, body: bytes, delivery: Delivery) -> tuple[httpx.Response, str]:
        token = await self.provider_token.get()
        headers = {**self.headers(delivery), "authorization": f"bearer {token}"}
        try:
            return await self.client.post(url, content=body, headers=headers), token
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc

    async def send(self, delivery: Delivery) -> Receipt:
        body = json.dumps(
            payload(delivery, self.config), ensure_ascii=False, separators=(",", ":")
        ).encode()
        if len(body) > MAX_PAYLOAD:
            raise DeliveryError(f"APNs payload is {len(body)} bytes; the limit is {MAX_PAYLOAD}")
        if not DEVICE_TOKEN.fullmatch(delivery.address):
            raise InvalidAddressError("not an APNs device token (hexadecimal)")
        url = f"{HOSTS[self.config.environment]}/3/device/{delivery.address}"
        response, token = await self.post(url, body, delivery)
        if reason_of(response) == STALE_SIGNATURE_REASON:
            self.provider_token.invalidate(token)
            response, token = await self.post(url, body, delivery)
        if response.is_success:
            return Receipt(provider_message_id=response.headers.get("apns-id"))
        reason = reason_of(response)
        detail = f"apns {response.status_code}: {reason}"
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = finite_delay(response.headers.get("retry-after"))
            raise DeliveryError(detail, retryable=True, retry_after=retry_after)
        if reason in CONFIG_REASONS:
            logger.error(
                "APNs refused the channel's configuration (%s); check bundle_id, environment "
                "and the signing key",
                reason,
                extra={"channel": self.name, "message_id": str(delivery.message_id)},
            )
            raise DeliveryError(detail)
        if response.status_code == 410 or reason in DEAD_TOKEN_REASONS:
            raise InvalidAddressError(detail)
        raise DeliveryError(detail)


def reason_of(response: httpx.Response) -> str:
    if response.is_success:
        return ""
    try:
        data: Any = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    reason = data.get("reason", "") if isinstance(data, dict) else ""
    return str(reason) or f"HTTP {response.status_code}"
