from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from cue_notify.core import compliance as legal
from cue_notify.core.enums import Importance
from cue_notify.core.policy import QuietHours
from cue_notify.db.models import Address, Category, Recipient
from cue_notify.services import composer
from cue_notify.settings import ComplianceSettings, PolicySettings, Settings

NEW_YORK = ZoneInfo("America/New_York")
TEXTING = Settings(
    environment="test",
    policy=PolicySettings(default_timezone="UTC", quiet_hours=None),
    compliance=ComplianceSettings(us_texting_rules=True),
)


def shopper(quiet: QuietHours | None, region: str = "US-FL") -> Recipient:
    preferences = {} if quiet is None else {"quiet_hours": quiet.model_dump(mode="json")}
    return Recipient(
        external_id="shopper",
        timezone="America/New_York",
        region=region,
        preferences=preferences,
        addresses=[Address(channel="sms", value="+15551230000", enabled=True)],
    )


def marketing(**fields: object) -> Category:
    values: dict[str, object] = {
        "key": "marketing",
        "respect_quiet_hours": True,
        "allow_unsubscribe": True,
        "marketing": True,
        **fields,
    }
    return Category(**values)


def local(moment: datetime) -> datetime:
    return moment.astimezone(NEW_YORK).replace(tzinfo=None)


def test_legal_hours_cannot_move_a_text_into_quiet_hours() -> None:
    evening = datetime(2026, 12, 1, 20, 30, tzinfo=NEW_YORK).astimezone(UTC)
    late_riser = QuietHours(start=time(22), end=time(8, 30))
    result = composer.allowed_send_time(
        evening, shopper(late_riser), marketing(), Importance.NORMAL, ["sms"], TEXTING
    )
    assert local(result) == datetime(2026, 12, 2, 8, 30)


def test_legal_hours_win_when_no_moment_satisfies_both() -> None:
    evening = datetime(2026, 12, 1, 20, 30, tzinfo=NEW_YORK).astimezone(UTC)
    always_quiet = QuietHours(start=time(7), end=time(22))
    result = composer.allowed_send_time(
        evening, shopper(always_quiet), marketing(), Importance.NORMAL, ["sms"], TEXTING
    )
    assert local(result) == datetime(2026, 12, 2, 8, 0)


def test_allowed_times_are_left_alone() -> None:
    noon = datetime(2026, 12, 2, 12, 0, tzinfo=NEW_YORK).astimezone(UTC)
    night = QuietHours(start=time(22), end=time(8))
    result = composer.allowed_send_time(
        noon, shopper(night), marketing(), Importance.NORMAL, ["sms"], TEXTING
    )
    assert result == noon


def test_critical_skips_quiet_hours_only_where_unsubscribing_is_impossible() -> None:
    late = datetime(2026, 12, 1, 23, 0, tzinfo=NEW_YORK).astimezone(UTC)
    night = QuietHours(start=time(22), end=time(8))
    security = Category(
        key="security", respect_quiet_hours=True, allow_unsubscribe=False, marketing=False
    )
    promo = marketing(marketing=False)
    person = shopper(night, region="DE")
    assert (
        composer.allowed_send_time(late, person, security, Importance.CRITICAL, ["sms"], TEXTING)
        == late
    )
    deferred = composer.allowed_send_time(
        late, person, promo, Importance.CRITICAL, ["sms"], TEXTING
    )
    assert local(deferred) == datetime(2026, 12, 2, 8, 0)


def test_texting_hours_apply_to_marketing_texts_only() -> None:
    late = datetime(2026, 12, 1, 23, 0, tzinfo=NEW_YORK).astimezone(UTC)
    person = shopper(None)
    assert composer.texting_window(person, marketing(), ["sms"], TEXTING) is legal.STATES["US-FL"]
    assert composer.texting_window(person, marketing(), ["push"], TEXTING) is None
    receipts = marketing(marketing=False, respect_quiet_hours=False)
    assert (
        composer.allowed_send_time(late, person, receipts, Importance.NORMAL, ["sms"], TEXTING)
        == late
    )
