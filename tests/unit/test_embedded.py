from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from cue_notify.core.enums import Importance
from cue_notify.core.policy import QuietHours
from cue_notify.db.models import Category, Recipient
from cue_notify.embedded import Action, Decision, Gate
from cue_notify.services import composer
from cue_notify.settings import PolicySettings, Settings

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def berlin(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=BERLIN).astimezone(UTC)


@pytest.fixture
def clock() -> Clock:
    return Clock(berlin(2026, 10, 20, 12, 0))


@pytest.fixture
def gate(clock: Clock) -> Iterator[Gate]:
    with Gate(
        quiet_hours=("22:00", "08:00"),
        caps={"marketing": [("P1D", 1)]},
        cooldowns={"reminders": "PT1H"},
        budgets={"billing-bot": [("P1D", 2)]},
        digests={"comments": {"window": "PT10M"}},
        essential=["security"],
        clock=clock,
    ) as opened:
        yield opened


def test_sends_now_outside_quiet_hours(gate: Gate, clock: Clock) -> None:
    decision = gate.check("ada", category="orders", timezone="Europe/Berlin")
    assert decision.action is Action.SEND
    assert decision.at == clock.now
    assert decision.reason is None
    assert decision.person == "ada"
    assert decision.importance is Importance.NORMAL


def test_quiet_hours_defer_to_their_end_in_the_persons_time_zone(gate: Gate, clock: Clock) -> None:
    clock.now = berlin(2026, 10, 20, 2, 0)
    decision = gate.check("ada", category="orders", timezone="Europe/Berlin")
    assert decision.action is Action.DEFER
    assert decision.reason == "quiet_hours"
    assert decision.at == berlin(2026, 10, 20, 8, 0)


def test_unknown_time_zone_falls_back_to_the_default(clock: Clock) -> None:
    clock.now = datetime(2026, 10, 20, 23, 0, tzinfo=UTC)
    with Gate(timezone="UTC", clock=clock) as gate:
        decision = gate.check("ada", category="orders", timezone="Mars/Olympus")
    assert decision.at == datetime(2026, 10, 21, 8, 0, tzinfo=UTC)


def test_library_send_time_matches_the_server(clock: Clock) -> None:
    moments = [berlin(2026, 10, 20, hour, 30) for hour in range(24)]
    settings = Settings(
        environment="test",
        policy=PolicySettings(
            default_timezone="UTC", quiet_hours=QuietHours(start=time(21), end=time(7, 15))
        ),
    )
    recipient = Recipient(external_id="ada", timezone="Europe/Berlin", preferences={})
    category = Category(
        key="orders", respect_quiet_hours=True, allow_unsubscribe=True, marketing=False
    )
    with Gate(quiet_hours=("21:00", "07:15"), clock=clock) as gate:
        for moment in moments:
            clock.now = moment
            library = gate.check("ada", category="orders", timezone="Europe/Berlin")
            server = composer.allowed_send_time(
                moment, recipient, category, Importance.NORMAL, ["push"], settings
            )
            assert library.at == server


def test_frequency_cap_suppresses_with_the_server_reason(gate: Gate) -> None:
    first = gate.check("ada", category="marketing", timezone="Europe/Berlin")
    second = gate.check("ada", category="marketing")
    assert first.action is Action.SEND
    assert second.action is Action.SUPPRESS
    assert second.reason == "frequency_cap: 1 per 1 day, 0:00:00"
    assert gate.check("grace", category="marketing").action is Action.SEND


def test_cooldown_suppresses_repeats_within_the_period(gate: Gate, clock: Clock) -> None:
    assert gate.check("ada", category="reminders").action is Action.SEND
    clock.now += timedelta(minutes=30)
    assert gate.check("ada", category="reminders").reason == "cooldown"
    clock.now += timedelta(minutes=31)
    assert gate.check("ada", category="reminders").action is Action.SEND


def test_deferred_messages_count_towards_caps(gate: Gate, clock: Clock) -> None:
    clock.now = berlin(2026, 10, 20, 2, 0)
    burst = [
        gate.check("ada", category="marketing", timezone="Europe/Berlin", key=f"promo-{n}")
        for n in range(5)
    ]
    assert [decision.action for decision in burst] == [Action.DEFER] + [Action.SUPPRESS] * 4
    assert all(decision.at == berlin(2026, 10, 20, 8, 0) for decision in burst)


def test_repeated_key_returns_the_same_decision(gate: Gate, clock: Clock) -> None:
    first = gate.check("ada", category="marketing", key="order-1001-shipped")
    clock.now += timedelta(minutes=5)
    again = gate.check("ada", category="marketing", key="order-1001-shipped")
    assert again == first
    capped = gate.check("ada", category="marketing", key="promo")
    assert gate.check("ada", category="marketing", key="promo") == capped


def test_sent_records_the_delivery(gate: Gate, clock: Clock) -> None:
    decision = gate.check("ada", category="orders", key="order-1")
    clock.now += timedelta(seconds=3)
    sent = gate.sent(decision)
    assert sent.sent_at == clock.now
    assert sent.action is Action.SEND
    assert gate.check("ada", category="orders", key="order-1").sent_at == clock.now
    assert gate.sent(decision).sent_at == clock.now


def test_only_sendable_decisions_can_be_marked_sent(gate: Gate) -> None:
    gate.check("ada", category="marketing")
    suppressed = gate.check("ada", category="marketing")
    with pytest.raises(ValueError, match="suppress"):
        gate.sent(suppressed)
    grouped = gate.check("ada", category="comments")
    with pytest.raises(ValueError, match="digest"):
        gate.sent(grouped)
    unknown = Decision(
        id="missing",
        action=Action.SEND,
        at=datetime(2026, 1, 1, tzinfo=UTC),
        reason=None,
        person="ada",
        category="orders",
        importance=Importance.NORMAL,
    )
    with pytest.raises(ValueError, match="unknown"):
        gate.sent(unknown)


def test_due_releases_deferred_messages_once(gate: Gate, clock: Clock) -> None:
    clock.now = berlin(2026, 10, 20, 2, 0)
    deferred = gate.check("ada", category="orders", timezone="Europe/Berlin", data={"id": 7})
    assert gate.due(berlin(2026, 10, 20, 7, 59)) == []
    released = gate.due(berlin(2026, 10, 20, 8, 0))
    assert [decision.id for decision in released] == [deferred.id]
    assert released[0].action is Action.SEND
    assert released[0].data == {"id": 7}
    assert gate.due(berlin(2026, 10, 20, 9, 0)) == []


def test_due_rechecks_quiet_hours_in_the_current_time_zone(gate: Gate, clock: Clock) -> None:
    clock.now = berlin(2026, 10, 20, 2, 0)
    deferred = gate.check("ada", category="orders", timezone="Europe/Berlin", key="a")
    evening = gate.check("ada", category="orders", timezone="America/New_York", key="b")
    assert evening.action is Action.SEND
    assert gate.due(berlin(2026, 10, 20, 8, 0)) == []
    moved = gate.check("ada", category="orders", key="a")
    assert moved.id == deferred.id
    assert moved.at == datetime(2026, 10, 20, 8, 0, tzinfo=NEW_YORK).astimezone(UTC)
    assert [decision.key for decision in gate.due(moved.at)] == ["a"]


def test_scheduled_messages_wait_for_their_time(gate: Gate, clock: Clock) -> None:
    later = clock.now + timedelta(hours=2)
    decision = gate.check("ada", category="orders", at=later)
    assert (decision.action, decision.reason, decision.at) == (Action.DEFER, "scheduled", later)
    past = gate.check("ada", category="orders", at=clock.now - timedelta(days=1))
    assert (past.action, past.at) == (Action.SEND, clock.now)
    assert [d.id for d in gate.due(later)] == [decision.id]


def test_expiry_suppresses_at_decision_and_at_release(gate: Gate, clock: Clock) -> None:
    clock.now = berlin(2026, 10, 20, 2, 0)
    too_late = gate.check("ada", category="orders", expires_at=berlin(2026, 10, 20, 7, 0))
    assert (too_late.action, too_late.reason) == (Action.SUPPRESS, "expired")
    clock.now = berlin(2026, 10, 20, 12, 0)
    gate.check("ada", category="orders", at=berlin(2026, 10, 20, 13, 0), key="x", expires_at=None)
    waiting = gate.check(
        "grace",
        category="orders",
        at=berlin(2026, 10, 20, 13, 0),
        expires_at=berlin(2026, 10, 20, 13, 30),
        key="y",
    )
    assert waiting.action is Action.DEFER
    released = gate.due(berlin(2026, 10, 20, 14, 0))
    assert [decision.key for decision in released] == ["x"]
    assert gate.check("grace", category="orders", key="y").reason == "expired"


def test_critical_breaks_through_only_in_essential_categories(gate: Gate, clock: Clock) -> None:
    clock.now = berlin(2026, 10, 20, 2, 0)
    alert = gate.check("ada", category="security", timezone="Europe/Berlin", importance="critical")
    assert (alert.action, alert.importance) == (Action.SEND, Importance.CRITICAL)
    promo = gate.check("ada", category="marketing", importance=Importance.CRITICAL)
    assert (promo.action, promo.importance) == (Action.DEFER, Importance.HIGH)


def test_critical_skips_caps_in_essential_categories(clock: Clock) -> None:
    with Gate(caps={"security": [("PT1H", 1)]}, essential=["security"], clock=clock) as gate:
        actions = [
            gate.check("ada", category="security", importance="critical").action for _ in range(3)
        ]
        assert actions == [Action.SEND] * 3
        assert gate.check("ada", category="security").action is Action.SUPPRESS


def test_senders_share_category_caps_but_have_their_own_budgets(clock: Clock) -> None:
    with Gate(
        caps={"alerts": [("P1D", 3)]},
        budgets={"triage-bot": [("P1D", 1)], "billing-bot": [("P1D", 1)]},
        clock=clock,
    ) as gate:
        first = gate.check("ada", category="alerts", sender="triage-bot")
        over_budget = gate.check("ada", category="alerts", sender="triage-bot")
        other = gate.check("ada", category="billing", sender="billing-bot")
        assert first.action is Action.SEND
        assert over_budget.reason == "agent_budget: 1 per 1 day, 0:00:00"
        assert other.action is Action.SEND
        assert gate.check("grace", category="alerts", sender="triage-bot").action is Action.SEND
        gate.check("ada", category="alerts")
        gate.check("ada", category="alerts")
        capped = gate.check("ada", category="alerts", sender="new-bot")
        assert capped.reason == "frequency_cap: 3 per 1 day, 0:00:00"


def test_digest_collects_items_into_one_due_batch(gate: Gate, clock: Clock) -> None:
    opened = clock.now
    grouped = [
        gate.check("ada", category="comments", data={"author": name}, key=f"c-{name}")
        for name in ("lin", "kay", "sam")
    ]
    assert {decision.action for decision in grouped} == {Action.DIGEST}
    assert {decision.at for decision in grouped} == {opened + timedelta(minutes=10)}
    assert gate.due(opened + timedelta(minutes=9)) == []
    [batch] = gate.due(opened + timedelta(minutes=10))
    assert batch.action is Action.SEND
    assert batch.count == 3
    assert [item.data["author"] for item in batch.items] == ["lin", "kay", "sam"]
    assert [item.key for item in batch.items] == ["c-lin", "c-kay", "c-sam"]
    assert gate.due(opened + timedelta(hours=1)) == []
    clock.now = opened + timedelta(minutes=11)
    assert gate.check("ada", category="comments").at == clock.now + timedelta(minutes=10)


def test_digest_batches_follow_policy_when_they_close(clock: Clock) -> None:
    clock.now = berlin(2026, 10, 20, 21, 55)
    with Gate(
        digests={"comments": {"window": "PT10M", "max_items": 2}},
        caps={"comments": [("P1D", 1)]},
        clock=clock,
        timezone="Europe/Berlin",
    ) as gate:
        for n in range(3):
            gate.check("ada", category="comments", data={"n": n})
        assert gate.due(berlin(2026, 10, 20, 22, 5)) == []
        [batch] = gate.due(berlin(2026, 10, 21, 8, 0))
        assert (batch.count, [item.data["n"] for item in batch.items]) == (3, [1, 2])
        assert batch.reason == "quiet_hours"
        clock.now = berlin(2026, 10, 21, 9, 0)
        gate.check("ada", category="comments")
        assert gate.due(berlin(2026, 10, 21, 9, 10)) == []


def test_digest_send_after_closes_the_batch_early(clock: Clock) -> None:
    with Gate(digests={"comments": {"window": "PT1H", "send_after": 3}}, clock=clock) as gate:
        gate.check("ada", category="comments")
        gate.check("ada", category="comments")
        third = gate.check("ada", category="comments")
        assert third.at == clock.now
        [batch] = gate.due()
        assert batch.count == 3


def test_daily_digest_goes_out_at_local_time(clock: Clock) -> None:
    with Gate(digests={"news": {"deliver_at": "09:00"}}, clock=clock) as gate:
        decision = gate.check("ada", category="news", timezone="America/New_York")
        assert decision.at == datetime(2026, 10, 20, 9, 0, tzinfo=NEW_YORK).astimezone(UTC)


def test_expired_digest_items_are_dropped(gate: Gate, clock: Clock) -> None:
    gate.check("ada", category="comments", expires_at=clock.now + timedelta(minutes=5), key="old")
    gate.check("ada", category="comments", key="new")
    [batch] = gate.due(clock.now + timedelta(minutes=10))
    assert [item.key for item in batch.items] == ["new"]
    assert gate.check("ada", category="comments", key="old").reason == "expired"


def test_fall_back_hour_resumes_in_the_right_repetition(clock: Clock) -> None:
    with Gate(quiet_hours=("00:00", "02:30"), clock=clock) as gate:
        clock.now = datetime(2026, 10, 25, 0, 10, tzinfo=UTC)
        first = gate.check("ada", category="orders", timezone="Europe/Berlin")
        clock.now = datetime(2026, 10, 25, 1, 10, tzinfo=UTC)
        second = gate.check("grace", category="orders", timezone="Europe/Berlin")
    assert first.at == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert second.at == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert second.action is Action.DEFER


def test_state_persists_in_a_sqlite_file(tmp_path: Path, clock: Clock) -> None:
    url = f"sqlite:///{tmp_path / 'cue.db'}"
    with Gate(url, caps={"marketing": [("P1D", 1)]}, clock=clock) as gate:
        first = gate.check("ada", category="marketing", key="promo-1")
    with Gate(url, caps={"marketing": [("P1D", 1)]}, clock=clock) as gate:
        assert gate.check("ada", category="marketing", key="promo-1") == first
        assert gate.check("ada", category="marketing").action is Action.SUPPRESS
    with Gate(tmp_path / "cue.db", clock=clock) as gate:
        assert gate.check("ada", category="marketing", key="promo-1").id == first.id


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"timezone": "Nowhere/City"}, "unknown time zone"),
        ({"digests": {"c": {"window": "PT1M", "key": "{{ x }}"}}}, "not supported"),
        ({"digests": {"c": {}}}, "window or deliver_at"),
        ({"cooldowns": {"c": 0}}, "longer than zero"),
        ({"caps": {"c": [("P1D", -1)]}}, "greater than or equal"),
        ({"quiet_hours": ("25:00", "08:00")}, "time"),
    ],
)
def test_invalid_configuration_is_rejected(options: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Gate(**options)


def test_server_database_urls_are_rejected() -> None:
    with pytest.raises(ValueError, match="SQLite"):
        Gate("postgresql://localhost/cue")


def test_unknown_importance_is_rejected(gate: Gate) -> None:
    with pytest.raises(ValueError, match="urgent"):
        gate.check("ada", category="orders", importance="urgent")


def test_importing_the_library_loads_no_server_code() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from cue_notify.embedded import Gate\n"
            "Gate().check('ada', category='orders')\n"
            "heavy = {'fastapi', 'starlette', 'uvicorn', 'sqlalchemy', 'httpx', 'mcp', 'alembic'}\n"
            "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy))\n",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"


def test_concurrent_checks_never_exceed_a_cap(tmp_path: Path, clock: Clock) -> None:
    url = f"sqlite:///{tmp_path / 'shared.db'}"
    gates = [Gate(url, caps={"marketing": [("P1D", 2)]}, clock=clock) for _ in range(4)]
    try:
        with ThreadPoolExecutor(max_workers=16) as pool:
            decisions = list(
                pool.map(
                    lambda n: gates[n % 4].check("ada", category="marketing"),
                    range(40),
                )
            )
    finally:
        for gate in gates:
            gate.close()
    actions = [decision.action for decision in decisions]
    assert actions.count(Action.SEND) == 2
    assert actions.count(Action.SUPPRESS) == 38
