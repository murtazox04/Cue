from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from cue_notify.core import policy
from cue_notify.core.enums import Importance, SuppressionReason
from cue_notify.core.policy import QuietHours

TASHKENT = ZoneInfo("Asia/Tashkent")  # UTC+5, no DST
NEW_YORK = ZoneInfo("America/New_York")
NIGHT = QuietHours(start=time(22), end=time(8))


@pytest.mark.parametrize(
    ("local", "expected_local"),
    [
        (datetime(2026, 3, 1, 12, 0), datetime(2026, 3, 1, 12, 0)),  # daytime: unchanged
        (datetime(2026, 3, 1, 23, 30), datetime(2026, 3, 2, 8, 0)),  # late: next morning
        (datetime(2026, 3, 1, 3, 15), datetime(2026, 3, 1, 8, 0)),  # early: same morning
        (datetime(2026, 3, 1, 8, 0), datetime(2026, 3, 1, 8, 0)),  # end is exclusive
        (datetime(2026, 3, 1, 22, 0), datetime(2026, 3, 2, 8, 0)),  # start is inclusive
    ],
)
def test_quiet_hours_wrapping_midnight(local: datetime, expected_local: datetime) -> None:
    when = local.replace(tzinfo=TASHKENT).astimezone(UTC)
    result = policy.defer_past_quiet_hours(when, TASHKENT, NIGHT)
    assert result.astimezone(TASHKENT).replace(tzinfo=None) == expected_local
    assert result.tzinfo == UTC


def test_quiet_hours_same_day_window_and_disabled() -> None:
    lunch = QuietHours(start=time(12), end=time(13))
    when = datetime(2026, 3, 1, 12, 30, tzinfo=UTC)
    assert policy.defer_past_quiet_hours(when, ZoneInfo("UTC"), lunch).hour == 13
    assert policy.defer_past_quiet_hours(when, ZoneInfo("UTC"), None) == when
    off = QuietHours(start=time(9), end=time(9))
    assert policy.defer_past_quiet_hours(when, ZoneInfo("UTC"), off) == when


def test_quiet_hours_across_dst_change() -> None:
    # 2026-03-08 02:00 is skipped in New York; the window still ends at 08:00 local time.
    when = datetime(2026, 3, 7, 23, 0, tzinfo=NEW_YORK).astimezone(UTC)
    result = policy.defer_past_quiet_hours(when, NEW_YORK, NIGHT).astimezone(NEW_YORK)
    assert (result.day, result.hour, result.utcoffset()) == (8, 8, NEW_YORK.utcoffset(result))


@pytest.mark.parametrize(
    ("available", "preferred", "default", "expected"),
    [
        (["en", "uz"], "uz-Latn-UZ", "en", "uz"),
        (["en", "uz-latn"], "uz-Latn-UZ", "en", "uz-latn"),
        (["en", "ru"], "uz", "ru", "ru"),
        (["en", "ru"], None, "de", "en"),
        (["pt_BR"], "pt-BR", "en", "pt_BR"),
        ([], "en", "en", None),
    ],
)
def test_pick_locale(
    available: list[str], preferred: str | None, default: str, expected: str | None
) -> None:
    assert policy.pick_locale(available, preferred, default) == expected


def test_rollout_is_deterministic_and_monotonic() -> None:
    users = [f"user-{index}" for index in range(2000)]
    at_10 = {u for u in users if policy.in_rollout("rule", u, 10)}
    at_50 = {u for u in users if policy.in_rollout("rule", u, 50)}
    assert at_10 <= at_50  # ramping up only adds people
    assert 120 < len(at_10) < 280
    assert 850 < len(at_50) < 1150
    assert at_10 == {u for u in users if policy.in_rollout("rule", u, 10)}
    assert all(policy.in_rollout("rule", u, 100) for u in users[:10])
    assert not any(policy.in_rollout("rule", u, 0) for u in users[:10])


def test_zone_falls_back_for_unknown_names() -> None:
    assert policy.zone("Mars/Olympus", "Asia/Tashkent") == TASHKENT
    assert policy.zone(None, "nope") == ZoneInfo("UTC")


def test_quiet_hours_ending_in_the_repeated_hour_never_resume_in_the_past() -> None:
    short_night = QuietHours(start=time(23), end=time(1, 30))
    second_ten_past_one = datetime(2026, 11, 1, 6, 10, tzinfo=UTC)
    result = policy.defer_past_quiet_hours(second_ten_past_one, NEW_YORK, short_night)
    assert result == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("offsets", "expected"),
    [
        ([], 0),
        ([timedelta(days=10)], 0),
        ([timedelta(days=-1), timedelta(days=1)], 0),
        ([timedelta(hours=-20), timedelta(hours=20)], 1),
        ([timedelta(hours=-1), timedelta(hours=1)], 2),
        ([timedelta(hours=-23), timedelta(hours=-22), timedelta(hours=20)], 2),
        ([timedelta(0), timedelta(hours=5), timedelta(hours=23)], 3),
    ],
)
def test_rolling_peak_counts_the_busiest_window_around_a_moment(
    offsets: list[timedelta], expected: int
) -> None:
    at = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    times = [at + offset for offset in offsets]
    assert policy.rolling_peak(times, at, timedelta(days=1)) == expected


def test_critical_is_honoured_only_where_people_cannot_opt_out() -> None:
    assert policy.honoured_importance(Importance.CRITICAL, unsubscribable=False) is (
        Importance.CRITICAL
    )
    assert policy.honoured_importance(Importance.CRITICAL, unsubscribable=True) is Importance.HIGH
    assert (
        policy.honoured_importance(
            Importance.CRITICAL, unsubscribable=False, ceiling=Importance.NORMAL
        )
        is Importance.NORMAL
    )
    assert policy.breaks_through(Importance.CRITICAL, unsubscribable=False)
    assert not policy.breaks_through(Importance.HIGH, unsubscribable=False)


def test_requested_times_in_the_past_mean_now() -> None:
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    hour = timedelta(hours=1)
    assert policy.earliest_send(now, now - hour, timedelta()) == now
    assert policy.earliest_send(now, now + hour, timedelta()) == now + hour
    assert policy.earliest_send(now, None, hour) == now + hour


def test_expiry_and_limit_reasons() -> None:
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    assert policy.expired(now, now)
    assert not policy.expired(now, None)
    assert not policy.expired(now, now + timedelta(seconds=1))
    cap = policy.FrequencyCap(period=timedelta(days=1), limit=3)
    assert policy.limit_reason(SuppressionReason.FREQUENCY_CAP, cap) == (
        "frequency_cap: 3 per 1 day, 0:00:00"
    )
