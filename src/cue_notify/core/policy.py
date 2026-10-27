"""Pure policy helpers: quiet hours, locale resolution, deterministic rollout.

Everything here is side-effect free so it can be unit-tested exhaustively; the services
layer combines these with database state.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer, field_validator

from cue_notify.core import compliance as legal
from cue_notify.core.enums import Importance, SuppressionReason

SEND_TIME_ROUNDS = 32


class QuietHours(BaseModel):
    """A daily window, in the recipient's local time, during which non-urgent messages wait.

    The window may wrap midnight (``22:00``-``08:00``). ``start == end`` disables it.
    """

    model_config = ConfigDict(frozen=True)

    start: time
    end: time


class FrequencyCap(BaseModel):
    """At most ``limit`` messages of a category per rolling ``period``."""

    model_config = ConfigDict(frozen=True)

    period: timedelta = Field(description="Rolling window, e.g. 'P1D' or 86400.")
    limit: int = Field(ge=0)


class Fatigue(BaseModel):
    """Back off from people who stopped paying attention.

    After ``after`` messages in a row that the person did not open, click or act on, Cue
    pauses this category's ``low`` and ``normal`` messages to them for ``pause``. Then one
    message gets through; any engagement ends the pause. ``high`` and ``critical``
    messages are never paused.

    Needs engagement reporting (``opened``/``clicked``), otherwise every message looks
    ignored.
    """

    model_config = ConfigDict(frozen=True)

    after: int = Field(ge=2, le=50, description="Unengaged messages in a row.")
    pause: timedelta = Field(description="How long to hold back, e.g. 'P7D'.")

    @field_validator("pause")
    @classmethod
    def _sensible_pause(cls, value: timedelta) -> timedelta:
        if not timedelta(hours=1) <= value <= timedelta(days=90):
            raise ValueError("pause must be between 1 hour and 90 days")
        return value


def zone(name: str | None, default: str) -> ZoneInfo:
    """Resolve an IANA time zone, falling back to ``default`` for unknown names."""
    for candidate in (name, default):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return ZoneInfo("UTC")


def _in_window(moment: time, window: QuietHours) -> bool:
    if window.start == window.end:
        return False
    if window.start < window.end:
        return window.start <= moment < window.end
    return moment >= window.start or moment < window.end


def defer_past_quiet_hours(when: datetime, tz: ZoneInfo, window: QuietHours | None) -> datetime:
    """Return ``when`` unchanged, or the end of the quiet window if ``when`` falls inside it.

    ``when`` must be timezone-aware; the result is in the same zone as ``when``.
    """
    if window is None:
        return when
    local = when.astimezone(tz)
    if not _in_window(local.time(), window):
        return when
    end_date = local.date()
    if window.start > window.end and local.time() >= window.start:
        end_date += timedelta(days=1)  # window wraps midnight: it ends tomorrow
    # Combining with tzinfo resolves DST gaps/folds the same way zoneinfo does elsewhere.
    resume = datetime.combine(end_date, window.end, tzinfo=tz)
    if resume.astimezone(UTC) <= when.astimezone(UTC):
        resume = resume.replace(fold=1)
    return resume.astimezone(when.tzinfo)


def send_time(
    when: datetime, tz: ZoneInfo, quiet: QuietHours | None, window: legal.Window | None
) -> datetime:
    earliest = when
    for _ in range(SEND_TIME_ROUNDS):
        moved = defer_past_quiet_hours(when, tz, quiet)
        if window is not None:
            moved = legal.next_allowed(moved, tz, window)
        if moved == when:
            return when
        when = moved
    return when if window is None else legal.next_allowed(earliest, tz, window)


def earliest_send(now: datetime, requested: datetime | None, delay: timedelta) -> datetime:
    return max(requested, now) if requested is not None else now + delay


def breaks_through(importance: Importance, *, unsubscribable: bool) -> bool:
    return importance is Importance.CRITICAL and not unsubscribable


def honoured_importance(
    importance: Importance, *, unsubscribable: bool, ceiling: Importance | None = None
) -> Importance:
    if ceiling is not None and importance.rank > ceiling.rank:
        importance = ceiling
    if importance is Importance.CRITICAL and unsubscribable:
        return Importance.HIGH
    return importance


def expired(at: datetime, expires_at: datetime | None) -> bool:
    return expires_at is not None and at >= expires_at


def limit_reason(reason: SuppressionReason, cap: FrequencyCap) -> str:
    return f"{reason}: {cap.limit} per {cap.period}"


def rolling_peak(times: Iterable[datetime], at: datetime, period: timedelta) -> int:
    nearby = sorted(moment for moment in times if at - period < moment < at + period)
    starts = [moment for moment in nearby if moment <= at] + [at]
    return max(bisect_left(nearby, start + period) - bisect_left(nearby, start) for start in starts)


def locale_candidates(preferred: str | None, default: str) -> list[str]:
    """``"uz-Latn-UZ"`` → ``["uz-latn-uz", "uz-latn", "uz", <default>…]`` (deduplicated)."""
    ordered: list[str] = []
    for tag in (preferred, default):
        if not tag:
            continue
        parts = tag.replace("_", "-").lower().split("-")
        for size in range(len(parts), 0, -1):
            candidate = "-".join(parts[:size])
            if candidate not in ordered:
                ordered.append(candidate)
    return ordered


def pick_locale(available: Iterable[str], preferred: str | None, default: str) -> str | None:
    """Choose the best available locale for a recipient, or ``None`` if nothing is available."""
    by_normalised = {tag.replace("_", "-").lower(): tag for tag in available}
    for candidate in locale_candidates(preferred, default):
        if candidate in by_normalised:
            return by_normalised[candidate]
    return next(iter(by_normalised.values()), None)


def in_rollout(rule_key: str, recipient_key: str, percent: float) -> bool:
    """Deterministically place a recipient inside or outside a percentage rollout.

    The same recipient always gets the same answer for the same rule, so ramping a rule
    from 10% to 50% only ever *adds* recipients.
    """
    if percent >= 100:
        return True
    if percent <= 0:
        return False
    digest = hashlib.sha256(f"{rule_key}\x00{recipient_key}".encode()).digest()
    bucket = int.from_bytes(digest[:8]) % 10_000
    return bucket < percent * 100


def first_reachable(planned: Sequence[str], reachable: Iterable[str]) -> list[str]:
    """Filter ``planned`` channels (keeping order) down to the ones that can be used."""
    available = set(reachable)
    return [channel for channel in planned if channel in available]


class RecipientPreferences(BaseModel):
    """Recipient-controlled delivery preferences."""

    unsubscribed_categories: set[str] = Field(default_factory=set)
    muted_channels: set[str] = Field(default_factory=set)
    quiet_hours: QuietHours | None = Field(
        default=None,
        description="Overrides the default quiet hours. Use equal start and end to disable.",
    )
    consent_expires: dict[str, AwareDatetime] = Field(
        default_factory=dict,
        description="Per category: the recorded opt-in stops counting after this moment.",
    )

    @field_serializer("unsubscribed_categories", "muted_channels")
    def _sorted(self, value: set[str]) -> list[str]:
        return sorted(value)  # stable JSON for storage, diffs and API responses
