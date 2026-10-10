"""Telegram Bot API channel. The address is the recipient's chat id."""

from __future__ import annotations

from html import escape
from typing import Any, ClassVar

import httpx
from pydantic import Field, SecretStr

from cue_notify.channels.base import (
    Channel,
    ChannelConfig,
    Delivery,
    DeliveryError,
    InvalidAddressError,
    Receipt,
    transport_error,
)
from cue_notify.core.enums import Importance

API = "https://api.telegram.org"


class TelegramConfig(ChannelConfig):
    bot_token: SecretStr
    disable_notification: bool = False
    link_preview: bool = False
    api_url: str = Field(default=API, description="Override for a self-hosted Bot API server.")


def format_message(title: str | None, body: str, url: str | None) -> str:
    parts = [f"<b>{escape(title)}</b>"] if title else []
    parts.append(escape(body))
    if url:
        parts.append(escape(url))
    return "\n\n".join(parts)


class TelegramChannel(Channel[TelegramConfig]):
    provider: ClassVar[str] = "telegram"
    config_model: ClassVar[type[ChannelConfig]] = TelegramConfig

    async def send(self, delivery: Delivery) -> Receipt:
        content = delivery.content
        token = self.config.bot_token.get_secret_value()
        payload: dict[str, Any] = {
            "chat_id": delivery.address,
            "text": format_message(content.title, content.body, content.url),
            "parse_mode": "HTML",
            "disable_notification": self.config.disable_notification
            or delivery.importance is Importance.LOW,
            "link_preview_options": {"is_disabled": not self.config.link_preview},
        }
        buttons = [{"text": a.label, "url": a.url} for a in content.actions if a.url]
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[button] for button in buttons]}
        try:
            response = await self.http.post(
                f"{self.config.api_url}/bot{token}/sendMessage", json=payload
            )
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc

        data = response.json() if response.content else {}
        if data.get("ok"):
            return Receipt(provider_message_id=str(data["result"]["message_id"]))
        description = str(data.get("description", response.text[:300]))
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = data.get("parameters", {}).get("retry_after")
            raise DeliveryError(description, retryable=True, retry_after=retry_after)
        if response.status_code in (400, 403) and (
            "chat not found" in description
            or "blocked" in description
            or "deactivated" in description
        ):
            raise InvalidAddressError(description)
        raise DeliveryError(description)
