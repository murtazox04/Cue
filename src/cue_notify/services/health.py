from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChannelState:
    failures: int = 0
    open_until: float = 0.0
    probe_started: float | None = None


@dataclass(slots=True)
class ChannelHealth:
    failures: int
    cooldown: float
    states: dict[str, ChannelState] = field(default_factory=dict)

    def available(self, channel: str, *, now: float | None = None) -> bool:
        state = self.states.get(channel)
        if state is None or not state.open_until:
            return True
        current = time.monotonic() if now is None else now
        if current < state.open_until:
            return False
        if state.probe_started is not None and current - state.probe_started < self.cooldown:
            return False
        state.probe_started = current
        return True

    def succeeded(self, channel: str) -> None:
        state = self.states.pop(channel, None)
        if state is not None and state.open_until:
            logger.info("channel %s is healthy again", channel, extra={"channel": channel})

    def failed(self, channel: str, *, now: float | None = None) -> None:
        if self.failures <= 0:
            return
        state = self.states.setdefault(channel, ChannelState())
        state.failures += 1
        state.probe_started = None
        if state.failures >= self.failures:
            state.open_until = (time.monotonic() if now is None else now) + self.cooldown
            logger.warning(
                "channel %s failed %d times in a row; routing around it for %.0f s",
                channel,
                state.failures,
                self.cooldown,
                extra={"channel": channel},
            )
