from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cue_notify.channels.base import Channel
from cue_notify.channels.webhook import WebhookConfig
from cue_notify.settings import Settings


def configuration_warnings(settings: Settings, channels: Mapping[str, Channel[Any]]) -> list[str]:
    warnings: list[str] = []
    if settings.api.plaintext_public_url:
        warnings.append(
            f"api.public_url ({settings.api.public_url}) uses plain http: preference, "
            "unsubscribe and review links would carry their tokens unencrypted; use https"
        )
    for name, channel in channels.items():
        config: object = channel.config
        if isinstance(config, WebhookConfig) and config.needs_secret:
            warnings.append(
                f"webhook channel {name!r} posts to a fixed url without a secret, so the "
                "receiver cannot verify requests come from Cue; set secret, or "
                "allow_unsigned = true to accept unsigned requests"
            )
    return warnings
