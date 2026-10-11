"""When the law lets marketing text messages reach someone, by jurisdiction.

US federal law (47 CFR 64.1200(c)(1)) forbids telephone solicitations, which include
marketing texts, before 8 a.m. or after 9 p.m. at the recipient's location. Several
states are stricter, with narrower hours, Sunday rules or a cap of three messages a day.
The table below covers the rules we verified against the statutes as of October 2026;
it is a safety net, not legal advice.

When a US recipient's state is unknown, the strict window applies: it satisfies every
state in the table at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

SUNDAY = 6


@dataclass(frozen=True, slots=True)
class Window:
    """Hours (recipient local time) when marketing texts may be sent."""

    start: time
    end: time
    source: str
    sunday: tuple[time, time] | None = None
    """Different hours on Sundays; ``no_sunday`` forbids Sundays altogether."""
    no_sunday: bool = False
    daily_cap: int | None = None
    """At most this many marketing texts per rolling 24 hours."""

    def hours(self, weekday: int) -> tuple[time, time] | None:
        if weekday == SUNDAY:
            if self.no_sunday:
                return None
            if self.sunday is not None:
                return self.sunday
        return self.start, self.end


def _h(hour: int) -> time:
    return time(hour, 0)


FEDERAL = Window(_h(8), _h(21), "47 CFR 64.1200(c)(1)")

STATES: dict[str, Window] = {
    "US-CT": Window(_h(9), _h(20), "Conn. Gen. Stat. 42-288a(c)"),
    "US-FL": Window(_h(8), _h(20), "Fla. Stat. 501.616(6)", daily_cap=3),
    "US-MD": Window(_h(8), _h(20), "Md. Code, Com. Law 14-4502(c)", daily_cap=3),
    "US-OK": Window(_h(8), _h(20), "Okla. Stat. tit. 15, 775C.4", daily_cap=3),
    "US-OR": Window(_h(8), _h(20), "Oregon HB 3865 (2025)", daily_cap=3),
    "US-TX": Window(_h(9), _h(21), "Tex. Bus. & Com. Code 301.051(b)(2)", sunday=(_h(12), _h(21))),
    "US-UT": Window(_h(8), _h(21), "Utah Code 13-25a-103(3)", no_sunday=True),
    "US-VA": Window(_h(8), _h(21), "Va. Code 59.1-511"),
    "US-WA": Window(_h(8), _h(20), "RCW 80.36.390(8)"),
}

STRICT = Window(
    _h(9),
    _h(20),
    "strictest US state rules (state unknown)",
    no_sunday=True,
    daily_cap=3,
)


def window_for(region: str | None, phone: str | None, *, unknown_state: str) -> Window | None:
    """The rules for a recipient, or ``None`` outside the US.

    ``region`` is an ISO 3166 code (``US``, ``US-FL``). Without one, a ``+1`` number is
    treated as a US recipient in an unknown state.
    """
    if region is not None:
        if region in STATES:
            return STATES[region]
        if region.startswith("US-"):
            return FEDERAL  # a state without stricter rules in the table
        if region != "US":
            return None
    elif not (phone or "").startswith("+1"):
        return None
    return STRICT if unknown_state == "strict" else FEDERAL


def next_allowed(when: datetime, tz: ZoneInfo, window: Window) -> datetime:
    """``when`` if it falls inside the window, else the next moment that does."""
    local = when.astimezone(tz)
    for offset in range(8):
        day = local.date() + timedelta(days=offset)
        hours = window.hours(day.weekday())
        if hours is None:
            continue
        start = datetime.combine(day, hours[0], tzinfo=tz)
        end = datetime.combine(day, hours[1], tzinfo=tz)
        if offset == 0 and start <= local < end:
            return when
        if local < start:
            return start.astimezone(when.tzinfo)
    raise AssertionError("every week has an allowed day")  # pragma: no cover
