from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cue_notify.embedded import Action, Decision, Gate


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Outbox:
    def __init__(self, gate: Gate) -> None:
        self.gate = gate
        self.delivered: list[tuple[datetime, Decision]] = []

    def deliver(self, decision: Decision, now: datetime) -> None:
        assert decision.action is Action.SEND
        self.delivered.append((now, decision))
        self.gate.sent(decision)


def test_a_night_across_the_autumn_clock_change(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 10, 25, 0, 30, tzinfo=UTC))
    settings = {
        "quiet_hours": ("22:00", "08:00"),
        "caps": {"marketing": [("P1D", 1)], "alerts": [("PT1H", 2)]},
        "budgets": {"deploy-bot": [("PT1H", 1)], "billing-bot": [("PT1H", 1)]},
        "digests": {"comments": {"window": "PT30M"}},
        "clock": clock,
    }
    database = f"sqlite:///{tmp_path / 'cue.db'}"
    gate = Gate(database, **settings)
    outbox = Outbox(gate)

    promos = [
        gate.check(
            "ada",
            category="marketing",
            timezone="Europe/Berlin",
            key=f"autumn-sale-{n}",
            data={"campaign": "autumn"},
        )
        for n in range(5)
    ]
    first_morning = datetime(2026, 10, 25, 7, 0, tzinfo=UTC)
    assert (promos[0].action, promos[0].at, promos[0].reason) == (
        Action.DEFER,
        first_morning,
        "quiet_hours",
    )
    assert {(p.action, p.reason) for p in promos[1:]} == {
        (Action.SUPPRESS, "frequency_cap: 1 per 1 day, 0:00:00")
    }

    clock.now = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    shipped = gate.check("ada", category="orders", key="order-1001-shipped", data={"order": 1001})
    assert (shipped.action, shipped.at) == (Action.DEFER, first_morning)
    gate.close()

    gate = Gate(database, **settings)
    outbox.gate = gate
    retried = gate.check("ada", category="orders", key="order-1001-shipped")
    assert retried == shipped

    for n, author in enumerate(["lin", "kay", "sam", "noor"]):
        decision = gate.check(
            "grace",
            category="comments",
            timezone="Asia/Tokyo",
            key=f"comment-{n}",
            data={"author": author},
        )
        assert decision.action is Action.DIGEST

    deploy = [
        gate.check("grace", category="alerts", sender="deploy-bot", key=f"deploy-{n}")
        for n in range(2)
    ]
    billing = gate.check("grace", category="alerts", sender="billing-bot", key="billing-0")
    triage = gate.check("grace", category="alerts", sender="triage-bot", key="triage-0")
    assert [d.action for d in deploy] == [Action.SEND, Action.SUPPRESS]
    assert deploy[1].reason == "agent_budget: 1 per 1:00:00"
    assert billing.action is Action.SEND
    assert (triage.action, triage.reason) == (Action.SUPPRESS, "frequency_cap: 2 per 1:00:00")
    for decision in (deploy[0], billing):
        outbox.deliver(decision, clock.now)

    while clock.now < datetime(2026, 10, 25, 8, 0, tzinfo=UTC):
        clock.now += timedelta(minutes=15)
        for decision in gate.due():
            outbox.deliver(decision, clock.now)

    timeline = [
        (when, decision.person, decision.category, decision.count)
        for when, decision in outbox.delivered
    ]
    assert timeline == [
        (datetime(2026, 10, 25, 1, 30, tzinfo=UTC), "grace", "alerts", 1),
        (datetime(2026, 10, 25, 1, 30, tzinfo=UTC), "grace", "alerts", 1),
        (datetime(2026, 10, 25, 2, 0, tzinfo=UTC), "grace", "comments", 4),
        (first_morning, "ada", "marketing", 1),
        (first_morning, "ada", "orders", 1),
    ]
    digest = outbox.delivered[2][1]
    assert [item.data["author"] for item in digest.items] == ["lin", "kay", "sam", "noor"]
    assert gate.check("ada", category="orders", key="order-1001-shipped").sent_at == first_morning
    assert gate.due(datetime(2026, 10, 26, tzinfo=UTC)) == []
    gate.close()
