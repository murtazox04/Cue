from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import time
from email.utils import formataddr
from typing import Any, ClassVar

import httpx
from pydantic import Field, SecretStr

from cue_notify.channels import email as mail
from cue_notify.channels.base import (
    Channel,
    ChannelConfig,
    Delivery,
    DeliveryError,
    Receipt,
    raise_for_http,
    transport_error,
)
from cue_notify.channels.smtp import single_address
from cue_notify.core.enums import FeedbackType

logger = logging.getLogger(__name__)

API = "https://api.resend.com"
WEBHOOK_TOLERANCE = 300
SIGNATURE_HEADERS = ("svix-id", "svix-timestamp", "svix-signature")
RETRYABLE_CONFLICTS = frozenset({"concurrent_idempotent_requests", "resource_locked"})


class ResendConfig(ChannelConfig):
    api_key: SecretStr
    from_address: str = Field(description="A sender on a domain verified in Resend.")
    from_name: str | None = None
    reply_to: str | None = None
    webhook_secret: SecretStr | None = Field(
        default=None, description="Signing secret of the Resend webhook (whsec_…)."
    )
    api_url: str = Field(default=API, description="Override for tests or a proxy.")


class ResendChannel(Channel[ResendConfig]):
    provider: ClassVar[str] = "resend"
    config_model: ClassVar[type[ChannelConfig]] = ResendConfig

    async def send(self, delivery: Delivery) -> Receipt:
        config = self.config
        payload: dict[str, Any] = {
            "from": formataddr((config.from_name or "", config.from_address)),
            "to": [single_address(delivery.address)],
            "subject": mail.subject(delivery),
            "text": mail.text_body(delivery),
            "headers": mail.headers(delivery),
            "tags": [{"name": "cue_message_id", "value": str(delivery.message_id)}],
        }
        if delivery.content.html:
            payload["html"] = delivery.content.html
        if config.reply_to:
            payload["reply_to"] = config.reply_to
        headers = {
            "Authorization": f"Bearer {config.api_key.get_secret_value()}",
            "Idempotency-Key": idempotency_key(delivery),
        }
        try:
            response = await self.http.post(
                f"{config.api_url}/emails", json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc
        data = json_object(response)
        if response.status_code == 409 and data.get("name") in RETRYABLE_CONFLICTS:
            raise DeliveryError(f"resend 409: {data.get('name')}", retryable=True)
        raise_for_http(response)
        if not data.get("id"):
            logger.warning(
                "resend accepted the message without an id",
                extra={"channel": self.name, "message_id": str(delivery.message_id)},
            )
            return Receipt()
        return Receipt(provider_message_id=str(data["id"]))


def idempotency_key(delivery: Delivery) -> str:
    address = hashlib.sha256(delivery.address.encode()).hexdigest()
    return f"{delivery.message_id}:{address}"


def json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        data: Any = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def verify_webhook(
    secret: str, headers: dict[str, str], body: bytes, *, now: float | None = None
) -> bool:
    msg_id = headers.get("svix-id", "")
    timestamp = headers.get("svix-timestamp", "")
    if not msg_id or not timestamp.isdigit():
        return False
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > WEBHOOK_TOLERANCE:
        return False
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except (binascii.Error, ValueError):
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest())
    for candidate in headers.get("svix-signature", "").split():
        version, _, signature = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(
            expected, signature.encode("utf-8", "surrogateescape")
        ):
            return True
    return False


def feedback(event: dict[str, Any]) -> tuple[FeedbackType, str, str, list[str]] | None:
    data: dict[str, Any] = event.get("data") or {}
    email_id = str(data.get("email_id") or "")
    raw_to = data.get("to")
    to = [str(a) for a in raw_to] if isinstance(raw_to, list) else []
    match event.get("type"):
        case "email.delivered":
            return FeedbackType.DELIVERED, "delivered", email_id, to
        case "email.bounced":
            bounce: dict[str, Any] = data.get("bounce") or {}
            reason = str(bounce.get("message") or bounce.get("subType") or "bounced")
            kind = (
                FeedbackType.BOUNCE
                if str(bounce.get("type", "Permanent")).lower() == "permanent"
                else FeedbackType.SOFT_BOUNCE
            )
            return kind, reason, email_id, to
        case "email.suppressed":
            return FeedbackType.BOUNCE, "suppressed by Resend", email_id, to
        case "email.complained":
            return FeedbackType.COMPLAINT, "marked as spam", email_id, to
        case _:
            return None
