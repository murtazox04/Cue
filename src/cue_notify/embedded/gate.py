from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, time, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import TypeAdapter

from cue_notify.core.digest import DigestPolicy
from cue_notify.core.enums import Importance
from cue_notify.core.policy import FrequencyCap, QuietHours
from cue_notify.core.time import utcnow
from cue_notify.embedded import store
from cue_notify.embedded.decision import Decision
from cue_notify.embedded.engine import Engine, Rules
from cue_notify.embedded.store import Store

Period = str | int | float | timedelta
Limit = tuple[Period, int]

PERIOD = TypeAdapter(timedelta)
UNSUPPORTED_DIGEST_FIELDS = ("key", "summarize", "summary_fields")


def limits(raw: Sequence[Limit]) -> tuple[FrequencyCap, ...]:
    return tuple(FrequencyCap.model_validate({"period": p, "limit": n}) for p, n in raw)


def cooldown(raw: Period) -> timedelta:
    period = PERIOD.validate_python(raw)
    if period <= timedelta():
        raise ValueError("a cooldown must be longer than zero")
    return period


def digest_policy(raw: Mapping[str, Any]) -> DigestPolicy:
    unsupported = [name for name in UNSUPPORTED_DIGEST_FIELDS if raw.get(name)]
    if unsupported:
        raise ValueError(f"digest fields not supported by the library: {', '.join(unsupported)}")
    return DigestPolicy.model_validate(dict(raw))


def valid_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone {name!r}") from exc
    return name


class Gate:
    def __init__(
        self,
        database: str | Path = store.MEMORY,
        *,
        quiet_hours: tuple[str | time, str | time] | None = ("22:00", "08:00"),
        timezone: str = "UTC",
        caps: Mapping[str, Sequence[Limit]] | None = None,
        cooldowns: Mapping[str, Period] | None = None,
        budgets: Mapping[str, Sequence[Limit]] | None = None,
        digests: Mapping[str, Mapping[str, Any]] | None = None,
        essential: Iterable[str] = (),
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        rules = Rules(
            quiet_hours=None
            if quiet_hours is None
            else QuietHours.model_validate({"start": quiet_hours[0], "end": quiet_hours[1]}),
            timezone=valid_timezone(timezone),
            caps={category: limits(raw) for category, raw in (caps or {}).items()},
            cooldowns={category: cooldown(raw) for category, raw in (cooldowns or {}).items()},
            budgets={sender: limits(raw) for sender, raw in (budgets or {}).items()},
            digests={category: digest_policy(raw) for category, raw in (digests or {}).items()},
            essential=frozenset(essential),
        )
        self.engine = Engine(rules, Store(database), clock)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.engine.store.close()

    def check(
        self,
        person: str,
        *,
        category: str,
        timezone: str | None = None,
        importance: Importance | str = Importance.NORMAL,
        key: str | None = None,
        sender: str | None = None,
        at: datetime | None = None,
        expires_at: datetime | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> Decision:
        return self.engine.check(
            person,
            category=category,
            timezone=timezone,
            importance=importance,
            key=key,
            sender=sender,
            at=at,
            expires_at=expires_at,
            data=data,
        )

    def sent(self, decision: Decision) -> Decision:
        return self.engine.sent(decision)

    def due(self, now: datetime | None = None) -> list[Decision]:
        return self.engine.due(now)
