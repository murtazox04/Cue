"""When digests close: after a window, or at a local time of day."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from cue_notify.core.digest import DigestPolicy

TASHKENT = ZoneInfo("Asia/Tashkent")
BERLIN = ZoneInfo("Europe/Berlin")


def test_window_closes_after_its_length() -> None:
    now = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    policy = DigestPolicy(window=timedelta(minutes=10))
    assert policy.closes_at(now, TASHKENT) == now + timedelta(minutes=10)


def test_daily_digest_goes_out_at_the_next_local_morning() -> None:
    policy = DigestPolicy.model_validate({"deliver_at": "08:00"})
    # 02:00 UTC is 07:00 in Tashkent: this morning's 08:00 is still ahead.
    early = datetime(2026, 10, 12, 2, 0, tzinfo=UTC)
    assert policy.closes_at(early, TASHKENT) == datetime(2026, 10, 12, 3, 0, tzinfo=UTC)
    # Exactly 08:00 local has passed for this digest: tomorrow.
    on_time = datetime(2026, 10, 12, 3, 0, tzinfo=UTC)
    assert policy.closes_at(on_time, TASHKENT) == datetime(2026, 10, 13, 3, 0, tzinfo=UTC)


def test_weekly_digest_waits_for_an_allowed_day() -> None:
    policy = DigestPolicy.model_validate({"deliver_at": "09:30", "days": ["mon"]})
    tuesday = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    assert policy.closes_at(tuesday, TASHKENT) == datetime(2026, 10, 19, 4, 30, tzinfo=UTC)
    monday_morning = datetime(2026, 10, 19, 4, 0, tzinfo=UTC)  # 09:00 local
    assert policy.closes_at(monday_morning, TASHKENT) == datetime(2026, 10, 19, 4, 30, tzinfo=UTC)


def test_local_time_follows_daylight_saving() -> None:
    policy = DigestPolicy.model_validate({"deliver_at": "08:00"})
    # Berlin leaves summer time on 25 October 2026: 08:00 is 06:00 UTC before, 07:00 after.
    assert policy.closes_at(datetime(2026, 10, 24, 12, 0, tzinfo=UTC), BERLIN) == datetime(
        2026, 10, 25, 7, 0, tzinfo=UTC
    )
    assert policy.closes_at(datetime(2026, 10, 23, 12, 0, tzinfo=UTC), BERLIN) == datetime(
        2026, 10, 24, 6, 0, tzinfo=UTC
    )
