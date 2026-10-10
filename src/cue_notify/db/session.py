"""Engine and session factory."""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from cue_notify.settings import DatabaseSettings

type SessionFactory = async_sessionmaker[AsyncSession]


def create_engine(settings: DatabaseSettings, *, default_pool_size: int = 10) -> AsyncEngine:
    if settings.is_sqlite:
        engine = create_async_engine(settings.url, echo=settings.echo)

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_connect(dbapi_connection: Any, _: Any) -> None:  # pyright: ignore[reportUnusedFunction]
            # Let SQLAlchemy, not the driver, manage transactions (see the "begin" hook).
            dbapi_connection.isolation_level = None
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.close()

        @event.listens_for(engine.sync_engine, "begin")
        def _sqlite_begin(connection: Any) -> None:  # pyright: ignore[reportUnusedFunction]
            # SQLite has no SELECT … FOR UPDATE. Taking the write lock up front serialises
            # transactions, which is what keeps frequency caps exact under concurrency.
            connection.exec_driver_sql("BEGIN IMMEDIATE")

        return engine

    return create_async_engine(
        settings.url,
        echo=settings.echo,
        pool_size=settings.pool_size or default_pool_size,
        max_overflow=settings.max_overflow,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
