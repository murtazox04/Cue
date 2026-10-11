"""Legal hours for marketing texts in the US."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from cue_notify.core.compliance import FEDERAL, STATES, STRICT, next_allowed, window_for

NEW_YORK = ZoneInfo("America/New_York")
CHICAGO = ZoneInfo("America/Chicago")


def test_jurisdiction_comes_from_the_region_or_a_us_number() -> None:
    assert window_for("US-FL", None, unknown_state="strict") is STATES["US-FL"]
    assert window_for("US-NY", None, unknown_state="strict") is FEDERAL
    assert window_for("US", None, unknown_state="strict") is STRICT
    assert window_for(None, "+15551234567", unknown_state="federal") is FEDERAL
    assert window_for("DE", "+15551234567", unknown_state="strict") is None
    assert window_for(None, "+998901234567", unknown_state="strict") is None


def test_messages_outside_the_window_wait_for_the_next_morning() -> None:
    florida = STATES["US-FL"]
    # 23:00 in New York on Tuesday 1 December 2026 (EST): next allowed is 08:00 Wednesday.
    late = datetime(2026, 12, 2, 4, 0, tzinfo=UTC)
    assert next_allowed(late, NEW_YORK, florida) == datetime(2026, 12, 2, 13, 0, tzinfo=UTC)
    # 07:00 local waits an hour; 12:00 local goes now.
    early = datetime(2026, 12, 2, 12, 0, tzinfo=UTC)
    assert next_allowed(early, NEW_YORK, florida) == datetime(2026, 12, 2, 13, 0, tzinfo=UTC)
    noon = datetime(2026, 12, 2, 17, 0, tzinfo=UTC)
    assert next_allowed(noon, NEW_YORK, florida) == noon
    # 20:30 local is past Florida's 8 p.m. but within the federal 9 p.m.
    evening = datetime(2026, 12, 3, 1, 30, tzinfo=UTC)
    assert next_allowed(evening, NEW_YORK, FEDERAL) == evening
    assert next_allowed(evening, NEW_YORK, florida) == datetime(2026, 12, 3, 13, 0, tzinfo=UTC)


def test_sunday_rules() -> None:
    sunday_morning = datetime(2026, 12, 6, 16, 0, tzinfo=UTC)  # 10:00 Sunday in Chicago
    # Texas: noon to 9 p.m. on Sundays.
    assert next_allowed(sunday_morning, CHICAGO, STATES["US-TX"]) == datetime(
        2026, 12, 6, 18, 0, tzinfo=UTC
    )
    # Utah and the strict default: not on Sundays at all; Monday 09:00 for strict.
    assert next_allowed(sunday_morning, CHICAGO, STRICT) == datetime(2026, 12, 7, 15, 0, tzinfo=UTC)
