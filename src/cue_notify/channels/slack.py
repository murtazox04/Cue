"""Slack through a bot's ``chat.postMessage``. The address is a member id (``U…``), which
Slack delivers as a direct message from the app, or a channel id (``C…``) the bot is in.

Needs a bot token (``xoxb-…``) with the ``chat:write`` scope (and ``im:write`` for
direct messages).
"""

from __future__ import annotations

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
    finite_delay,
    transport_error,
)
from cue_notify.core.enums import Importance

API = "https://slack.com/api"

# The conversation or person is gone, or the bot may not reach it: retrying won't help.
DEAD_ADDRESS_ERRORS = frozenset(
    {
        "channel_not_found",
        "user_not_found",
        "is_archived",
        "account_inactive",
        "user_disabled",
        "not_in_channel",
        "cannot_dm_bot",
        "messages_tab_disabled",
    }
)
TRANSIENT_ERRORS = frozenset(
    {"ratelimited", "internal_error", "fatal_error", "service_unavailable", "request_timeout"}
)
MAX_BUTTONS = 25  # one actions block


class SlackConfig(ChannelConfig):
    bot_token: SecretStr
    api_url: str = Field(default=API, description="Override for tests or a proxy.")
    unfurl_links: bool = Field(default=False, description="Let Slack preview links.")


def _mrkdwn(text: str) -> str:
    """Slack's three control characters, escaped so event data cannot ping @channel."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plain(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def blocks(delivery: Delivery) -> list[dict[str, Any]]:
    content = delivery.content
    result: list[dict[str, Any]] = []
    if content.title:
        title = content.title
        if delivery.importance is Importance.CRITICAL:
            title = f"🚨 {title}"
        result.append(
            {"type": "header", "text": {"type": "plain_text", "text": _plain(title, 150)}}
        )
    result.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": _plain(_mrkdwn(content.body), 3000)}}
    )
    if content.image_url:
        result.append(
            {"type": "image", "image_url": content.image_url, "alt_text": content.title or "image"}
        )
    buttons: list[dict[str, Any]] = []
    if content.url:
        buttons.append(_button("Open", content.url, "cue_open"))
    buttons += [
        _button(action.label, action.url, f"cue_{action.id}")
        for action in content.actions
        if action.url
    ]
    if buttons:
        result.append({"type": "actions", "elements": buttons[:MAX_BUTTONS]})
    if preferences := delivery.links.get("preferences"):
        result.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"<{preferences}|Notification settings>"}],
            }
        )
    return result


def _button(label: str, url: str, action_id: str) -> dict[str, Any]:
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": _plain(label, 75)},
        "url": url,
        "action_id": action_id[:255],
    }


class SlackChannel(Channel[SlackConfig]):
    provider: ClassVar[str] = "slack"
    config_model: ClassVar[type[ChannelConfig]] = SlackConfig

    async def send(self, delivery: Delivery) -> Receipt:
        content = delivery.content
        fallback = f"{content.title}: {content.body}" if content.title else content.body
        payload: dict[str, Any] = {
            "channel": delivery.address,
            "text": _plain(_mrkdwn(fallback), 3000),  # notifications and screen readers
            "blocks": blocks(delivery),
            "unfurl_links": self.config.unfurl_links,
            "unfurl_media": self.config.unfurl_links,
            "metadata": {
                "event_type": "cue_message",
                "event_payload": {k: str(v) for k, v in delivery.metadata.items()},
            },
        }
        headers = {"Authorization": f"Bearer {self.config.bot_token.get_secret_value()}"}
        try:
            response = await self.http.post(
                f"{self.config.api_url}/chat.postMessage", json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise transport_error(exc) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise DeliveryError(
                f"slack HTTP {response.status_code}",
                retryable=True,
                retry_after=finite_delay(response.headers.get("retry-after")),
            )
        data: dict[str, Any] = response.json() if response.content else {}
        if data.get("ok"):
            channel = data.get("channel", "")
            return Receipt(provider_message_id=f"{channel}:{data.get('ts', '')}")
        error = str(data.get("error") or f"HTTP {response.status_code}")
        if error in TRANSIENT_ERRORS:
            raise DeliveryError(f"slack: {error}", retryable=True)
        if error in DEAD_ADDRESS_ERRORS:
            raise InvalidAddressError(f"slack: {error}")
        raise DeliveryError(f"slack: {error}")
