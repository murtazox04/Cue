"""Logs messages instead of sending them — for local development and demos."""

from __future__ import annotations

import logging
from typing import ClassVar

from cue_notify.channels.base import Channel, ChannelConfig, Delivery, Receipt

logger = logging.getLogger("cue_notify.channels.console")


class ConsoleConfig(ChannelConfig):
    pass


class ConsoleChannel(Channel[ConsoleConfig]):
    provider: ClassVar[str] = "console"
    config_model: ClassVar[type[ChannelConfig]] = ConsoleConfig

    async def send(self, delivery: Delivery) -> Receipt:
        content = delivery.content
        logger.info(
            "[%s → %s] %s%s",
            self.name,
            delivery.address,
            f"{content.title} — " if content.title else "",
            content.body,
            extra={"message_id": str(delivery.message_id)},
        )
        return Receipt(provider_message_id=f"console-{delivery.message_id}")
