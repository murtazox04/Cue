from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cue_notify.embedded import Action, Decision, Gate

BERLIN = ZoneInfo("Europe/Berlin")


class SimulatedClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def local(moment: datetime) -> str:
    return f"{moment.astimezone(BERLIN):%H:%M %Z}"


def deliver(gate: Gate, decision: Decision) -> None:
    authors = ", ".join(str(item.data["author"]) for item in decision.items)
    detail = f": {decision.count} comments from {authors}" if decision.items else ""
    print(f"  sent     {decision.category:<10} {decision.key or '-':<20}{detail}".rstrip())
    gate.sent(decision)


def handle(gate: Gate, decision: Decision) -> None:
    if decision.action is Action.SEND:
        deliver(gate, decision)
        return
    until = "" if decision.action is Action.SUPPRESS else f" until {local(decision.at)}"
    reason = f"  ({decision.reason})" if decision.reason else ""
    label = f"  {decision.action:<8} {decision.category:<10} {decision.key or '-':<20}"
    print(f"{label}{until}{reason}")


def main() -> None:
    clock = SimulatedClock(datetime(2026, 10, 25, 0, 0, tzinfo=UTC))
    with tempfile.TemporaryDirectory() as workdir:
        gate = Gate(
            f"sqlite:///{Path(workdir) / 'cue.db'}",
            quiet_hours=("22:00", "08:00"),
            caps={"marketing": [("P1D", 1)]},
            budgets={"deploy-bot": [("PT1H", 2)]},
            digests={"comments": {"window": "PT30M"}},
            essential=["security"],
            clock=clock,
        )
        with gate:
            print(f"{local(clock.now)}, the night the clocks go back in Berlin")
            for n in range(3):
                handle(
                    gate,
                    gate.check(
                        "ada", category="marketing", timezone="Europe/Berlin", key=f"sale-{n}"
                    ),
                )
            handle(gate, gate.check("ada", category="orders", key="order-1001-shipped"))
            handle(gate, gate.check("ada", category="orders", key="order-1001-shipped"))
            handle(
                gate,
                gate.check("ada", category="security", importance="critical", key="new-login"),
            )
            for n in range(3):
                handle(
                    gate,
                    gate.check("ada", category="deploys", sender="deploy-bot", key=f"deploy-{n}"),
                )
            for n, author in enumerate(["lin", "kay", "sam"]):
                handle(
                    gate,
                    gate.check(
                        "ada", category="comments", key=f"comment-{n}", data={"author": author}
                    ),
                )

            print("\nA worker calls gate.due() every 30 minutes until 09:00")
            while clock.now < datetime(2026, 10, 25, 8, 0, tzinfo=UTC):
                clock.now += timedelta(minutes=30)
                released = gate.due()
                if released:
                    print(local(clock.now))
                for decision in released:
                    deliver(gate, decision)


if __name__ == "__main__":
    main()
