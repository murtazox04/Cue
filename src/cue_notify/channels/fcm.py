"""Firebase Cloud Messaging (HTTP v1 API). The address is an FCM registration token.

Authenticates with a Google service account using the OAuth 2.0 JWT-bearer flow, so the
heavyweight ``firebase-admin``/``google-auth`` SDKs are not needed. Requires the ``fcm``
extra (``pip install 'cue-notify[fcm]'``) for RS256 signing.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, ClassVar, Literal, Self

import httpx
from pydantic import BaseModel, Field, FilePath, SecretStr, model_validator

from cue_notify.channels.base import (
    Channel,
    ChannelConfig,
    Delivery,
    DeliveryError,
    InvalidAddressError,
    Receipt,
    flatten_data,
    transport_error,
)
from cue_notify.core.enums import Importance
from cue_notify.core.time import utcnow

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
# iOS 15+ interruption levels. "critical" needs an Apple entitlement and a critical sound,
# so Cue's critical maps to time-sensitive, which still breaks through Focus modes.
INTERRUPTION_LEVELS = {
    Importance.LOW: "passive",
    Importance.NORMAL: "active",
    Importance.HIGH: "time-sensitive",
    Importance.CRITICAL: "time-sensitive",
}
SEND_URL = "https://fcm.googleapis.com/v1/projects/{project}/messages:send"


class ServiceAccount(BaseModel):
    project_id: str
    client_email: str
    private_key: SecretStr
    token_uri: str = "https://oauth2.googleapis.com/token"  # noqa: S105 — a URL


class FcmConfig(ChannelConfig):
    credentials_file: FilePath | None = Field(
        default=None, description="Path to the service-account JSON key."
    )
    credentials_json: SecretStr | None = Field(
        default=None, description="Service-account JSON inline (e.g. from a secret store)."
    )
    project_id: str | None = Field(default=None, description="Defaults to the key's project.")
    android_priority: Literal["normal", "high"] = "high"
    sound: str | None = "default"
    ttl_seconds: int | None = Field(default=None, ge=0, le=2_419_200)

    @model_validator(mode="after")
    def _one_credential_source(self) -> Self:
        if (self.credentials_file is None) == (self.credentials_json is None):
            raise ValueError("set exactly one of credentials_file or credentials_json")
        return self

    def service_account(self) -> ServiceAccount:
        raw = (
            Path(self.credentials_file).read_text()
            if self.credentials_file
            else self.credentials_json.get_secret_value()  # type: ignore[union-attr]
        )
        return ServiceAccount.model_validate(json.loads(raw))


class _AccessToken:
    """Caches a short-lived OAuth access token and refreshes it ahead of expiry."""

    def __init__(self, account: ServiceAccount, http: httpx.AsyncClient) -> None:
        self._account = account
        self._http = http
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._token = None

    async def get(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._expires_at - 300:
                return self._token
            try:
                import jwt
            except ImportError as exc:  # pragma: no cover
                raise DeliveryError("FCM requires: pip install 'cue-notify[fcm]'") from exc
            now = int(time.time())
            assertion = jwt.encode(
                {
                    "iss": self._account.client_email,
                    "scope": SCOPE,
                    "aud": self._account.token_uri,
                    "iat": now,
                    "exp": now + 3600,
                },
                self._account.private_key.get_secret_value(),
                algorithm="RS256",
            )
            try:
                response = await self._http.post(
                    self._account.token_uri,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                )
            except httpx.HTTPError as exc:
                raise transport_error(exc) from exc
            if not response.is_success:
                raise DeliveryError(
                    f"FCM auth failed: HTTP {response.status_code} {response.text[:200]}",
                    retryable=response.status_code >= 500,
                )
            body = response.json()
            self._token = str(body["access_token"])
            self._expires_at = time.time() + float(body.get("expires_in", 3600))
            return self._token


class FcmChannel(Channel[FcmConfig]):
    provider: ClassVar[str] = "fcm"
    config_model: ClassVar[type[ChannelConfig]] = FcmConfig

    def __init__(self, name: str, config: FcmConfig, http: httpx.AsyncClient) -> None:
        super().__init__(name, config, http)
        account = config.service_account()
        self._project = config.project_id or account.project_id
        self._token = _AccessToken(account, http)

    def build(self, delivery: Delivery) -> dict[str, Any]:
        content = delivery.content
        notification: dict[str, Any] = {"body": content.body}
        if content.title:
            notification["title"] = content.title
        if content.image_url:
            notification["image"] = content.image_url
        data = flatten_data({**content.data, **delivery.metadata})
        if content.url:
            data.setdefault("url", content.url)
        if content.thread:
            data.setdefault("thread", content.thread)  # Android apps group by this key
        if content.actions:
            data.setdefault(
                "actions", json.dumps([a.model_dump(exclude_none=True) for a in content.actions])
            )
        data.setdefault("importance", delivery.importance.value)
        android: dict[str, Any] = {"priority": self._android_priority(delivery.importance)}
        ttl = self._ttl(delivery)
        if ttl is not None:
            android["ttl"] = f"{ttl}s"
        if self.config.sound:
            android["notification"] = {"sound": self.config.sound}
        message: dict[str, Any] = {
            "token": delivery.address,
            "notification": notification,
            "data": data,
            "android": android,
        }
        aps: dict[str, Any] = {"interruption-level": INTERRUPTION_LEVELS[delivery.importance]}
        if self.config.sound and delivery.importance is not Importance.LOW:
            aps["sound"] = self.config.sound
        if content.thread:
            aps["thread-id"] = content.thread  # iOS stacks notifications by thread
        apns: dict[str, Any] = {"payload": {"aps": aps}}
        if delivery.expires_at is not None:
            apns["headers"] = {"apns-expiration": str(int(delivery.expires_at.timestamp()))}
        message["apns"] = apns
        return {"message": message}

    def _android_priority(self, importance: Importance) -> str:
        if importance in (Importance.HIGH, Importance.CRITICAL):
            return "HIGH"
        if importance is Importance.LOW:
            return "NORMAL"
        return self.config.android_priority.upper()

    def _ttl(self, delivery: Delivery) -> int | None:
        """Seconds the push service should keep trying: the configured TTL, shortened to
        the message's expiry so a stale message is never shown."""
        ttl = self.config.ttl_seconds
        if delivery.expires_at is not None:
            remaining = max(int((delivery.expires_at - utcnow()).total_seconds()), 0)
            ttl = remaining if ttl is None else min(ttl, remaining)
        return ttl

    async def _post(self, url: str, payload: dict[str, Any]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self._token.get()}"}
        try:
            return await self.http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc

    async def send(self, delivery: Delivery) -> Receipt:
        payload = self.build(delivery)
        url = SEND_URL.format(project=self._project)
        response = await self._post(url, payload)
        if response.status_code == 401:
            self._token.invalidate()  # token revoked or clock skew — refresh once
            response = await self._post(url, payload)
        if response.is_success:
            return Receipt(provider_message_id=response.json().get("name"))
        raise classify_error(response)


def classify_error(response: httpx.Response) -> DeliveryError:
    try:
        error = response.json().get("error", {})
    except ValueError:
        error = {}
    codes = {
        detail.get("errorCode") for detail in error.get("details", []) if isinstance(detail, dict)
    }
    message = f"FCM {response.status_code} {error.get('status', '')}: {error.get('message', '')}"
    if "UNREGISTERED" in codes or "SENDER_ID_MISMATCH" in codes:
        return InvalidAddressError(message)
    if "INVALID_ARGUMENT" in codes and "registration token" in str(error.get("message", "")):
        return InvalidAddressError(message)
    if response.status_code == 429 or response.status_code >= 500:
        retry_after = response.headers.get("retry-after")
        return DeliveryError(
            message,
            retryable=True,
            retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
        )
    return DeliveryError(message)
