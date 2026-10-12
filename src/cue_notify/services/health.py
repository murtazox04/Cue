"""Channel health: stop waiting on a provider that keeps failing.

Each process counts consecutive transient failures per channel (timeouts, 5xx, rate
limits). After ``delivery.breaker_failures`` in a row the channel is *open* for
``delivery.breaker_cooldown_seconds``: messages that have another channel planned go
there at once instead of retrying the failing one. When the cooldown is over, the next
message tries the channel again; one success closes it.

A channel that is a message's only route is always tried, open or not, so health never
drops a message that could still get through.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _State:
    failures: int = 0
    open_until: float = 0.0


@dataclass(slots=True)
class ChannelHealth:
    failures: int
    """Consecutive failures that open a channel; 0 disables the breaker."""
    cooldown: float
    _states: dict[str, _State] = field(default_factory=dict)

    def available(self, channel: str, *, now: float | None = None) -> bool:
        state = self._states.get(channel)
        return state is None or state.open_until <= (time.monotonic() if now is None else now)

    def succeeded(self, channel: str) -> None:
        state = self._states.pop(channel, None)
        if state is not None and state.open_until:
            logger.info("channel %s is healthy again", channel, extra={"channel": channel})

    def failed(self, channel: str, *, now: float | None = None) -> None:
        if self.failures <= 0:
            return
        state = self._states.setdefault(channel, _State())
        state.failures += 1
        if state.failures >= self.failures:
            state.open_until = (time.monotonic() if now is None else now) + self.cooldown
            logger.warning(
                "channel %s failed %d times in a row; routing around it for %.0f s",
                channel,
                state.failures,
                self.cooldown,
                extra={"channel": channel},
            )

    def snapshot(self) -> dict[str, dict[str, float]]:
        now = time.monotonic()
        return {
            name: {"failures": s.failures, "open_for": max(s.open_until - now, 0.0)}
            for name, s in self._states.items()
        }
