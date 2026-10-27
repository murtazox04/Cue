from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from cue_notify.core import policy
from cue_notify.core.digest import DigestPolicy
from cue_notify.core.enums import Importance, SuppressionReason
from cue_notify.core.policy import FrequencyCap, QuietHours
from cue_notify.core.time import ensure_utc
from cue_notify.embedded import store
from cue_notify.embedded.decision import DIGEST, QUIET_HOURS, SCHEDULED, Action, Decision
from cue_notify.embedded.store import Batch, Record, Store


@dataclass(frozen=True, slots=True)
class Rules:
    quiet_hours: QuietHours | None
    timezone: str
    caps: Mapping[str, tuple[FrequencyCap, ...]]
    cooldowns: Mapping[str, timedelta]
    budgets: Mapping[str, tuple[FrequencyCap, ...]]
    digests: Mapping[str, DigestPolicy]
    essential: frozenset[str]


@dataclass(frozen=True, slots=True)
class Verdict:
    action: Action
    at: datetime
    reason: str | None


class Engine:
    def __init__(self, rules: Rules, storage: Store, clock: Callable[[], datetime]) -> None:
        self.rules = rules
        self.store = storage
        self.clock = clock

    def check(
        self,
        person: str,
        *,
        category: str,
        timezone: str | None,
        importance: Importance | str,
        key: str | None,
        sender: str | None,
        at: datetime | None,
        expires_at: datetime | None,
        data: Mapping[str, Any] | None,
    ) -> Decision:
        now = ensure_utc(self.clock())
        requested = Importance(importance)
        with self.store.transaction():
            if key is not None:
                existing = self.store.by_key(key)
                if existing is not None:
                    return self.decision(existing)
            if timezone is not None:
                self.store.remember(person, timezone)
            level = policy.honoured_importance(
                requested, unsubscribable=category not in self.rules.essential
            )
            entry = Record(
                id=uuid4().hex,
                key=key,
                person=person,
                category=category,
                sender=sender,
                importance=level.value,
                action=Action.SEND,
                state=store.READY,
                at=now,
                reason=None,
                data=dict(data or {}),
                batch=None,
                count=1,
                expires_at=None if expires_at is None else ensure_utc(expires_at),
                created=now,
                sent_at=None,
            )
            digest = self.rules.digests.get(category)
            if digest is not None:
                entry = self.group(entry, digest, now)
            else:
                start = policy.earliest_send(
                    now, None if at is None else ensure_utc(at), timedelta()
                )
                verdict = self.evaluate(entry, start, now)
                entry = self.settled(entry, verdict)
            self.store.insert(entry)
            return self.decision(entry)

    def sent(self, decision: Decision) -> Decision:
        now = ensure_utc(self.clock())
        with self.store.transaction():
            entry = self.store.by_id(decision.id)
            if entry is None:
                raise ValueError(f"unknown decision {decision.id}")
            if entry.state not in (store.READY, store.WAITING, store.SENT):
                raise ValueError(f"a decision to {entry.action} cannot be marked sent")
            if entry.state != store.SENT:
                self.store.mark_sent(entry.id, now)
            updated = self.store.by_id(entry.id)
            assert updated is not None
            return self.decision(updated)

    def due(self, now: datetime | None) -> list[Decision]:
        moment = ensure_utc(self.clock() if now is None else now)
        released: list[Record] = []
        with self.store.transaction():
            for batch in self.store.closing_batches(moment):
                entry = self.flush(batch, moment)
                if entry is not None and entry.state == store.READY:
                    released.append(entry)
            for entry in self.store.waiting(moment):
                ready = self.release(entry, moment)
                if ready is not None and ready.state == store.READY:
                    released.append(ready)
            return [self.decision(entry) for entry in sorted(released, key=lambda e: e.at)]

    def zone(self, person: str) -> ZoneInfo:
        return policy.zone(self.store.timezone(person), self.rules.timezone)

    def breaks_through(self, entry: Record) -> bool:
        return policy.breaks_through(
            Importance(entry.importance), unsubscribable=entry.category not in self.rules.essential
        )

    def quiet(self, entry: Record) -> QuietHours | None:
        return None if self.breaks_through(entry) else self.rules.quiet_hours

    def peak(
        self,
        entry: Record,
        *,
        by_category: bool,
        by_sender: bool,
        at: datetime,
        period: timedelta,
    ) -> int:
        times = self.store.counted_times(
            entry.person,
            category=entry.category if by_category else None,
            sender=entry.sender if by_sender else None,
            around=at,
            period=period,
        )
        return policy.rolling_peak(times, at, period)

    def evaluate(self, entry: Record, start: datetime, now: datetime) -> Verdict:
        when = policy.send_time(start, self.zone(entry.person), self.quiet(entry), None)
        period = self.rules.cooldowns.get(entry.category)
        if period is not None and self.peak(
            entry, by_category=True, by_sender=False, at=when, period=period
        ):
            return Verdict(Action.SUPPRESS, when, SuppressionReason.COOLDOWN)
        if policy.expired(when, entry.expires_at):
            return Verdict(Action.SUPPRESS, when, SuppressionReason.EXPIRED)
        caps = () if self.breaks_through(entry) else self.rules.caps.get(entry.category, ())
        for cap in caps:
            recent = self.peak(entry, by_category=True, by_sender=False, at=when, period=cap.period)
            if recent >= cap.limit:
                return Verdict(
                    Action.SUPPRESS,
                    when,
                    policy.limit_reason(SuppressionReason.FREQUENCY_CAP, cap),
                )
        budget = () if entry.sender is None else self.rules.budgets.get(entry.sender, ())
        for cap in budget:
            recent = self.peak(entry, by_category=False, by_sender=True, at=when, period=cap.period)
            if recent >= cap.limit:
                return Verdict(
                    Action.SUPPRESS,
                    when,
                    policy.limit_reason(SuppressionReason.AGENT_BUDGET, cap),
                )
        if when > start:
            return Verdict(Action.DEFER, when, QUIET_HOURS)
        if when > now:
            return Verdict(Action.DEFER, when, SCHEDULED)
        return Verdict(Action.SEND, when, None)

    def settled(self, entry: Record, verdict: Verdict) -> Record:
        state = {
            Action.SEND: store.READY,
            Action.DEFER: store.WAITING,
            Action.SUPPRESS: store.SUPPRESSED,
        }[verdict.action]
        return replace(
            entry, action=verdict.action, state=state, at=verdict.at, reason=verdict.reason
        )

    def group(self, entry: Record, digest: DigestPolicy, now: datetime) -> Record:
        batch = self.store.open_batch(entry.person, entry.category, entry.sender)
        if batch is None:
            batch = Batch(
                id=uuid4().hex,
                person=entry.person,
                category=entry.category,
                sender=entry.sender,
                closes_at=digest.closes_at(now, self.zone(entry.person)),
                count=0,
            )
        count = batch.count + 1
        closes_at = (
            now if digest.send_after is not None and count >= digest.send_after else batch.closes_at
        )
        batch = Batch(
            id=batch.id,
            person=batch.person,
            category=batch.category,
            sender=batch.sender,
            closes_at=min(closes_at, batch.closes_at),
            count=count,
        )
        self.store.save_batch(batch)
        return replace(
            entry,
            action=Action.DIGEST,
            state=store.GROUPED,
            at=batch.closes_at,
            reason=DIGEST,
            batch=batch.id,
        )

    def flush(self, batch: Batch, now: datetime) -> Record | None:
        self.store.close_batch(batch.id)
        live: list[Record] = []
        for member in self.store.members(batch.id):
            if policy.expired(now, member.expires_at):
                self.store.settle(
                    member.id,
                    action=Action.SUPPRESS,
                    state=store.SUPPRESSED,
                    at=member.at,
                    reason=SuppressionReason.EXPIRED,
                )
            else:
                live.append(member)
        if not live:
            return None
        importance = max((Importance(member.importance) for member in live), key=lambda i: i.rank)
        entry = Record(
            id=uuid4().hex,
            key=None,
            person=batch.person,
            category=batch.category,
            sender=batch.sender,
            importance=importance.value,
            action=Action.SEND,
            state=store.READY,
            at=now,
            reason=None,
            data={},
            batch=batch.id,
            count=len(live),
            expires_at=None,
            created=now,
            sent_at=None,
        )
        entry = self.settled(entry, self.evaluate(entry, now, now))
        self.store.insert(entry)
        return entry

    def release(self, entry: Record, now: datetime) -> Record | None:
        if policy.expired(now, entry.expires_at):
            self.store.settle(
                entry.id,
                action=Action.SUPPRESS,
                state=store.SUPPRESSED,
                at=entry.at,
                reason=SuppressionReason.EXPIRED,
            )
            return None
        allowed = policy.send_time(now, self.zone(entry.person), self.quiet(entry), None)
        if policy.expired(allowed, entry.expires_at):
            self.store.settle(
                entry.id,
                action=Action.SUPPRESS,
                state=store.SUPPRESSED,
                at=allowed,
                reason=SuppressionReason.EXPIRED,
            )
            return None
        if allowed > now:
            self.store.settle(
                entry.id, action=Action.DEFER, state=store.WAITING, at=allowed, reason=QUIET_HOURS
            )
            return None
        self.store.settle(
            entry.id, action=Action.SEND, state=store.READY, at=entry.at, reason=entry.reason
        )
        return self.store.by_id(entry.id)

    def decision(self, entry: Record) -> Decision:
        items: tuple[Decision, ...] = ()
        if entry.batch is not None and entry.action != Action.DIGEST:
            grouped = self.store.members(entry.batch)
            members = [member for member in grouped if member.state == store.GROUPED]
            digest = self.rules.digests.get(entry.category)
            if digest is not None:
                members = members[-digest.max_items :]
            items = tuple(self.decision(member) for member in members)
        return Decision(
            id=entry.id,
            action=Action(entry.action),
            at=entry.at,
            reason=entry.reason,
            person=entry.person,
            category=entry.category,
            importance=Importance(entry.importance),
            sender=entry.sender,
            key=entry.key,
            data=entry.data,
            items=items,
            count=entry.count,
            sent_at=entry.sent_at,
        )
