from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

API = "https://slack.com/api"

DEAD_ADDRESS_ERRORS = frozenset(
    {"channel_not_found", "is_archived", "not_in_channel", "messages_tab_disabled"}
)
CHANNEL_CONFIG_ERRORS = frozenset(
    {
        "account_inactive",
        "invalid_auth",
        "not_authed",
        "token_revoked",
        "token_expired",
        "missing_scope",
        "team_access_not_granted",
        "not_allowed_token_type",
    }
)
TRANSIENT_ERRORS = frozenset(
    {
        "ratelimited",
        "rate_limited",
        "internal_error",
        "fatal_error",
        "service_unavailable",
        "request_timeout",
    }
)
MAX_BUTTONS = 25
MAX_TEXT = 3000
MAX_HEADER = 150
MAX_BUTTON_LABEL = 75
MAX_URL = 3000
MAX_ALT_TEXT = 2000
MAX_ACTION_ID = 255
ELLIPSIS = "…"
ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


class SlackConfig(ChannelConfig):
    bot_token: SecretStr
    api_url: str = Field(default=API, description="Override for tests or a proxy.")
    unfurl_links: bool = Field(default=False, description="Let Slack preview links.")


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + ELLIPSIS


def escaped(text: str, limit: int) -> str:
    whole = "".join(ESCAPES.get(char, char) for char in text)
    if len(whole) <= limit:
        return whole
    kept: list[str] = []
    size = 0
    for char in text:
        piece = ESCAPES.get(char, char)
        if size + len(piece) > limit - len(ELLIPSIS):
            break
        kept.append(piece)
        size += len(piece)
    return "".join(kept) + ELLIPSIS


def mrkdwn(text: str) -> dict[str, Any]:
    return {"type": "mrkdwn", "text": text, "verbatim": True}


def blocks(delivery: Delivery) -> list[dict[str, Any]]:
    content = delivery.content
    result: list[dict[str, Any]] = []
    if content.title:
        title = content.title
        if delivery.importance is Importance.CRITICAL:
            title = f"🚨 {title}"
        result.append(
            {"type": "header", "text": {"type": "plain_text", "text": shorten(title, MAX_HEADER)}}
        )
    result.append({"type": "section", "text": mrkdwn(escaped(content.body, MAX_TEXT))})
    if content.image_url and len(content.image_url) <= MAX_URL:
        result.append(
            {
                "type": "image",
                "image_url": content.image_url,
                "alt_text": shorten(content.title or "image", MAX_ALT_TEXT),
            }
        )
    targets: list[tuple[str, str, str]] = []
    if content.url:
        targets.append(("Open", content.url, "cue_open"))
    targets += [(a.label, a.url, f"cue_{a.id}") for a in content.actions if a.url]
    buttons = [
        button(label, url, action_id)
        for label, url, action_id in unique_ids(
            [t for t in targets if len(t[1]) <= MAX_URL][:MAX_BUTTONS]
        )
    ]
    if buttons:
        result.append({"type": "actions", "elements": buttons})
    preferences = delivery.links.get("preferences")
    if preferences and len(preferences) <= MAX_URL:
        result.append(
            {"type": "context", "elements": [mrkdwn(f"<{preferences}|Notification settings>")]}
        )
    return result


def unique_ids(targets: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str, str]] = []
    for index, (label, url, action_id) in enumerate(targets):
        candidate = action_id[:MAX_ACTION_ID]
        if candidate in seen:
            suffix = f"_{index}"
            candidate = action_id[: MAX_ACTION_ID - len(suffix)] + suffix
        seen.add(candidate)
        result.append((label, url, candidate))
    return result


def button(label: str, url: str, action_id: str) -> dict[str, Any]:
    return {
        "type": "button",
        "text": {"type": "plain_text", "text": shorten(label, MAX_BUTTON_LABEL)},
        "url": url,
        "action_id": action_id,
    }


class SlackChannel(Channel[SlackConfig]):
    provider: ClassVar[str] = "slack"
    config_model: ClassVar[type[ChannelConfig]] = SlackConfig

    async def send(self, delivery: Delivery) -> Receipt:
        content = delivery.content
        fallback = f"{content.title}: {content.body}" if content.title else content.body
        payload: dict[str, Any] = {
            "channel": delivery.address,
            "text": escaped(fallback, MAX_TEXT),
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

        retry_after = finite_delay(response.headers.get("retry-after"))
        if response.status_code == 429 or response.status_code >= 500:
            raise DeliveryError(
                f"slack HTTP {response.status_code}", retryable=True, retry_after=retry_after
            )
        try:
            data: Any = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            if response.is_success:
                logger.warning(
                    "slack accepted the message without a JSON answer",
                    extra={"channel": self.name, "message_id": str(delivery.message_id)},
                )
                return Receipt()
            raise DeliveryError(f"slack HTTP {response.status_code}")
        answer: dict[str, Any] = data
        if answer.get("ok"):
            channel = answer.get("channel", "")
            return Receipt(provider_message_id=f"{channel}:{answer.get('ts', '')}")
        error = str(answer.get("error") or f"HTTP {response.status_code}")
        if error in TRANSIENT_ERRORS:
            raise DeliveryError(f"slack: {error}", retryable=True, retry_after=retry_after)
        if error in DEAD_ADDRESS_ERRORS:
            raise InvalidAddressError(f"slack: {error}")
        if error in CHANNEL_CONFIG_ERRORS:
            logger.error(
                "slack refused the bot token: %s",
                error,
                extra={"channel": self.name, "message_id": str(delivery.message_id)},
            )
        raise DeliveryError(f"slack: {error}")
