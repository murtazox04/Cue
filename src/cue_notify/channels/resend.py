"""E-mail through Resend's API. The address is an e-mail address.

Resend reports deliveries, bounces and spam complaints to a webhook: point one at
``<api.public_url>/v1/inbound/resend/<channel>`` and set ``webhook_secret`` to its
signing secret (``whsec_…``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
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

API = "https://api.resend.com"
WEBHOOK_TOLERANCE = 300
SIGNATURE_HEADERS = ("svix-id", "svix-timestamp", "svix-signature")


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
        sender = (
            f"{config.from_name} <{config.from_address}>"
            if config.from_name
            else (config.from_address)
        )
        payload: dict[str, Any] = {
            "from": sender,
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
            # Resend drops a repeat within 24 hours, so a retried job cannot send twice.
            "Idempotency-Key": str(delivery.message_id),
        }
        try:
            response = await self.http.post(
                f"{config.api_url}/emails", json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc
        raise_for_http(response)
        data: dict[str, Any] = response.json() if response.content else {}
        if not data.get("id"):
            raise DeliveryError("resend: no id in the response")
        return Receipt(provider_message_id=str(data["id"]))


def verify_webhook(
    secret: str, headers: dict[str, str], body: bytes, *, now: float | None = None
) -> bool:
    """Resend signs webhooks the Svix way: HMAC-SHA256 over ``id.timestamp.body`` with
    the base64 part of ``whsec_…``; ``svix-signature`` lists ``v1,<base64>`` values."""
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
    """(type, reason, Resend email id, addresses) for the events Cue acts on."""
    data: dict[str, Any] = event.get("data") or {}
    email_id = str(data.get("email_id") or "")
    raw_to = data.get("to")
    to = [str(a) for a in raw_to] if isinstance(raw_to, list) else []  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
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
        case "email.delivery_delayed":
            return FeedbackType.SOFT_BOUNCE, "delivery delayed", email_id, to
        case "email.suppressed":
            # Resend refuses the address after an earlier hard bounce or complaint.
            return FeedbackType.BOUNCE, "suppressed by Resend", email_id, to
        case "email.complained":
            return FeedbackType.COMPLAINT, "marked as spam", email_id, to
        case _:
            return None
