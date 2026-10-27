from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cue_notify.core.time import ensure_utc

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MICROSECOND = timedelta(microseconds=1)
MEMORY = ":memory:"
SCHEMA_VERSION = "1"

WAITING = "waiting"
READY = "ready"
SENT = "sent"
SUPPRESSED = "suppressed"
GROUPED = "grouped"
COUNTED = (WAITING, READY, SENT)

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS cue_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS cue_people (person TEXT PRIMARY KEY, timezone TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS cue_decisions (
        id TEXT PRIMARY KEY,
        key TEXT UNIQUE,
        person TEXT NOT NULL,
        category TEXT NOT NULL,
        sender TEXT,
        importance TEXT NOT NULL,
        action TEXT NOT NULL,
        state TEXT NOT NULL,
        at INTEGER NOT NULL,
        reason TEXT,
        data TEXT NOT NULL,
        batch TEXT,
        count INTEGER NOT NULL,
        expires_at INTEGER,
        created INTEGER NOT NULL,
        sent_at INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS cue_decisions_person ON cue_decisions (person, state, at)",
    "CREATE INDEX IF NOT EXISTS cue_decisions_state ON cue_decisions (state, at)",
    "CREATE INDEX IF NOT EXISTS cue_decisions_batch ON cue_decisions (batch, state)",
    """CREATE TABLE IF NOT EXISTS cue_batches (
        id TEXT PRIMARY KEY,
        person TEXT NOT NULL,
        category TEXT NOT NULL,
        sender TEXT,
        open INTEGER NOT NULL,
        closes_at INTEGER NOT NULL,
        count INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS cue_batches_open ON cue_batches (open, closes_at)",
)

COLUMNS = (
    "id, key, person, category, sender, importance, action, state, at, reason, data, "
    "batch, count, expires_at, created, sent_at"
)


def micros(value: datetime) -> int:
    return (ensure_utc(value) - EPOCH) // MICROSECOND


def instant(value: int) -> datetime:
    return EPOCH + value * MICROSECOND


def optional_micros(value: datetime | None) -> int | None:
    return None if value is None else micros(value)


def optional_instant(value: int | None) -> datetime | None:
    return None if value is None else instant(value)


def database_path(database: str | Path) -> str:
    if isinstance(database, Path):
        return str(database)
    if database in ("", MEMORY, "sqlite://", "sqlite:///:memory:"):
        return MEMORY
    if database.startswith("sqlite:///"):
        return database.removeprefix("sqlite:///")
    if "://" in database:
        raise ValueError(
            "the library keeps its state in SQLite: pass a file path or 'sqlite:///file.db'"
        )
    return database


@dataclass(frozen=True, slots=True)
class Record:
    id: str
    key: str | None
    person: str
    category: str
    sender: str | None
    importance: str
    action: str
    state: str
    at: datetime
    reason: str | None
    data: Mapping[str, Any]
    batch: str | None
    count: int
    expires_at: datetime | None
    created: datetime
    sent_at: datetime | None


@dataclass(frozen=True, slots=True)
class Batch:
    id: str
    person: str
    category: str
    sender: str | None
    closes_at: datetime
    count: int


def record(row: sqlite3.Row) -> Record:
    return Record(
        id=row["id"],
        key=row["key"],
        person=row["person"],
        category=row["category"],
        sender=row["sender"],
        importance=row["importance"],
        action=row["action"],
        state=row["state"],
        at=instant(row["at"]),
        reason=row["reason"],
        data=json.loads(row["data"]),
        batch=row["batch"],
        count=row["count"],
        expires_at=optional_instant(row["expires_at"]),
        created=instant(row["created"]),
        sent_at=optional_instant(row["sent_at"]),
    )


def batch(row: sqlite3.Row) -> Batch:
    return Batch(
        id=row["id"],
        person=row["person"],
        category=row["category"],
        sender=row["sender"],
        closes_at=instant(row["closes_at"]),
        count=row["count"],
    )


class Store:
    def __init__(self, database: str | Path) -> None:
        path = database_path(database)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(
            path, isolation_level=None, check_same_thread=False, timeout=30
        )
        self.connection.row_factory = sqlite3.Row
        if path != MEMORY:
            self.connection.execute("PRAGMA journal_mode=WAL")
        with self.transaction():
            for statement in SCHEMA:
                self.connection.execute(statement)
            found = self.connection.execute(
                "SELECT value FROM cue_meta WHERE name = 'schema'"
            ).fetchone()
            if found is None:
                self.connection.execute(
                    "INSERT INTO cue_meta (name, value) VALUES ('schema', ?)", (SCHEMA_VERSION,)
                )
            elif found["value"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {found['value']} is not supported by this version of Cue"
                )

    @contextmanager
    def transaction(self) -> Generator[None]:
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            self.connection.execute("COMMIT")

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def timezone(self, person: str) -> str | None:
        row = self.connection.execute(
            "SELECT timezone FROM cue_people WHERE person = ?", (person,)
        ).fetchone()
        return None if row is None else row["timezone"]

    def remember(self, person: str, timezone: str) -> None:
        self.connection.execute(
            "INSERT INTO cue_people (person, timezone) VALUES (?, ?) "
            "ON CONFLICT (person) DO UPDATE SET timezone = excluded.timezone",
            (person, timezone),
        )

    def by_key(self, key: str) -> Record | None:
        row = self.connection.execute(
            f"SELECT {COLUMNS} FROM cue_decisions WHERE key = ?",
            (key,),
        ).fetchone()
        return None if row is None else record(row)

    def by_id(self, decision_id: str) -> Record | None:
        row = self.connection.execute(
            f"SELECT {COLUMNS} FROM cue_decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()
        return None if row is None else record(row)

    def insert(self, entry: Record) -> None:
        self.connection.execute(
            f"INSERT INTO cue_decisions ({COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entry.id,
                entry.key,
                entry.person,
                entry.category,
                entry.sender,
                entry.importance,
                entry.action,
                entry.state,
                micros(entry.at),
                entry.reason,
                json.dumps(dict(entry.data)),
                entry.batch,
                entry.count,
                optional_micros(entry.expires_at),
                micros(entry.created),
                optional_micros(entry.sent_at),
            ),
        )

    def settle(
        self, decision_id: str, *, action: str, state: str, at: datetime, reason: str | None
    ) -> None:
        self.connection.execute(
            "UPDATE cue_decisions SET action = ?, state = ?, at = ?, reason = ? WHERE id = ?",
            (action, state, micros(at), reason, decision_id),
        )

    def mark_sent(self, decision_id: str, at: datetime) -> None:
        self.connection.execute(
            "UPDATE cue_decisions SET state = ?, sent_at = ? WHERE id = ?",
            (SENT, micros(at), decision_id),
        )

    def counted_times(
        self,
        person: str,
        *,
        category: str | None,
        sender: str | None,
        around: datetime,
        period: timedelta,
    ) -> list[datetime]:
        conditions = ["person = ?", f"state IN ({', '.join('?' for _ in COUNTED)})"]
        parameters: list[Any] = [person, *COUNTED]
        if category is not None:
            conditions.append("category = ?")
            parameters.append(category)
        if sender is not None:
            conditions.append("sender = ?")
            parameters.append(sender)
        conditions.extend(["at > ?", "at < ?"])
        parameters.extend([micros(around - period), micros(around + period)])
        rows = self.connection.execute(
            f"SELECT at FROM cue_decisions WHERE {' AND '.join(conditions)}",
            parameters,
        ).fetchall()
        return [instant(row["at"]) for row in rows]

    def waiting(self, now: datetime) -> list[Record]:
        rows = self.connection.execute(
            f"SELECT {COLUMNS} FROM cue_decisions WHERE state = ? AND at <= ? ORDER BY at, created",
            (WAITING, micros(now)),
        ).fetchall()
        return [record(row) for row in rows]

    def open_batch(self, person: str, category: str, sender: str | None) -> Batch | None:
        row = self.connection.execute(
            "SELECT id, person, category, sender, closes_at, count FROM cue_batches "
            "WHERE open = 1 AND person = ? AND category = ? AND sender IS ?",
            (person, category, sender),
        ).fetchone()
        return None if row is None else batch(row)

    def save_batch(self, entry: Batch) -> None:
        self.connection.execute(
            "INSERT INTO cue_batches (id, person, category, sender, open, closes_at, count) "
            "VALUES (?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET closes_at = excluded.closes_at, "
            "count = excluded.count",
            (
                entry.id,
                entry.person,
                entry.category,
                entry.sender,
                micros(entry.closes_at),
                entry.count,
            ),
        )
        self.connection.execute(
            "UPDATE cue_decisions SET at = ? WHERE batch = ? AND state = ?",
            (micros(entry.closes_at), entry.id, GROUPED),
        )

    def closing_batches(self, now: datetime) -> list[Batch]:
        rows = self.connection.execute(
            "SELECT id, person, category, sender, closes_at, count FROM cue_batches "
            "WHERE open = 1 AND closes_at <= ? ORDER BY closes_at",
            (micros(now),),
        ).fetchall()
        return [batch(row) for row in rows]

    def close_batch(self, batch_id: str) -> None:
        self.connection.execute("UPDATE cue_batches SET open = 0 WHERE id = ?", (batch_id,))

    def members(self, batch_id: str) -> list[Record]:
        rows = self.connection.execute(
            f"SELECT {COLUMNS} FROM cue_decisions "
            "WHERE batch = ? AND action = 'digest' ORDER BY created, rowid",
            (batch_id,),
        ).fetchall()
        return [record(row) for row in rows]
