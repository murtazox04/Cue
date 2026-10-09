"""Dialect-aware statements that portable SQLAlchemy Core does not cover."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase


def _insert(session: AsyncSession, model: type[DeclarativeBase]) -> Any:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return postgresql.insert(model)
    if dialect == "sqlite":
        return sqlite.insert(model)
    raise NotImplementedError(f"unsupported database dialect {dialect!r}")  # pragma: no cover


async def insert_ignore(
    session: AsyncSession,
    model: type[DeclarativeBase],
    values: Mapping[str, Any],
    conflict: Sequence[str],
) -> bool:
    """``INSERT … ON CONFLICT DO NOTHING``. Returns whether a row was inserted."""
    stmt = (
        _insert(session, model)
        .values(dict(values))
        .on_conflict_do_nothing(index_elements=list(conflict))
    )
    result = await session.execute(stmt)
    return bool(getattr(result, "rowcount", 0))


async def upsert_increment(
    session: AsyncSession,
    model: type[DeclarativeBase],
    key: Mapping[str, Any],
    increments: Mapping[str, int],
) -> None:
    """Insert ``key`` with ``increments`` as initial values, or atomically add to them."""
    stmt = _insert(session, model).values({**key, **increments})
    table = model.__table__
    stmt = stmt.on_conflict_do_update(
        index_elements=list(key),
        set_={name: table.c[name] + stmt.excluded[name] for name in increments},
    )
    await session.execute(stmt)
