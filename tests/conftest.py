"""Shared fixtures.

Tests run against SQLite by default. Set ``CUE_TEST_DATABASE_URL`` to a PostgreSQL
URL (``postgresql+asyncpg://…``) to run the same suite against PostgreSQL, as CI does.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from cue_notify.api.app import create_app
from cue_notify.core.enums import Scope
from cue_notify.db.migrations import alembic_config
from cue_notify.db.models import Category
from cue_notify.runtime import Runtime
from cue_notify.services import api_keys
from cue_notify.settings import (
    DatabaseSettings,
    PolicySettings,
    Settings,
    WorkerSettings,
)
from tests.helpers import FakeChannel

POSTGRES_URL = os.environ.get("CUE_TEST_DATABASE_URL")
TABLES = (
    "jobs, digests, preference_changes, review_links, approvals, messages, events, broadcasts, "
    "rules, templates, addresses, recipients, api_keys, agents, ai_usage"
)


# --------------------------------------------------------------------------- database


@pytest.fixture(scope="session")
def sqlite_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A migrated SQLite database, copied for every test (much faster than migrating)."""
    path = tmp_path_factory.mktemp("db") / "template.db"
    command.upgrade(alembic_config(f"sqlite+aiosqlite:///{path}"), "head")
    return path


@pytest.fixture(scope="session")
def postgres_migrated() -> Iterator[str | None]:
    if not POSTGRES_URL:
        yield None
        return

    async def reset() -> None:
        engine = create_async_engine(POSTGRES_URL)
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(reset())
    command.upgrade(alembic_config(POSTGRES_URL), "head")
    yield POSTGRES_URL


@pytest.fixture(scope="session")
def seeded_categories(postgres_migrated: str | None) -> list[dict[str, Any]]:
    """The categories created by migrations, restored after every PostgreSQL test."""
    if not postgres_migrated:
        return []

    async def read() -> list[dict[str, Any]]:
        engine = create_async_engine(postgres_migrated)
        async with engine.connect() as connection:
            rows = (await connection.execute(select(Category.__table__))).mappings().all()
        await engine.dispose()
        return [dict(row) for row in rows]

    return asyncio.run(read())


@pytest.fixture
def database_url(tmp_path: Path, sqlite_template: Path, postgres_migrated: str | None) -> str:
    if postgres_migrated:
        return postgres_migrated
    target = tmp_path / "cue_notify.db"
    shutil.copy(sqlite_template, target)
    return f"sqlite+aiosqlite:///{target}"


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        environment="test",
        database=DatabaseSettings(url=database_url, pool_size=5),
        worker=WorkerSettings(concurrency=4, retry_base_seconds=1, retry_max_seconds=2),
        policy=PolicySettings(default_locale="en", default_timezone="UTC", quiet_hours=None),
    )


@pytest.fixture
def push() -> FakeChannel:
    return FakeChannel(name="push")


@pytest.fixture
def sms() -> FakeChannel:
    return FakeChannel(name="sms")


@pytest.fixture(autouse=True)
async def _isolate_postgres(
    postgres_migrated: str | None, seeded_categories: list[dict[str, Any]]
) -> AsyncIterator[None]:
    """Leave PostgreSQL exactly as migrations left it after each test."""
    yield
    if not postgres_migrated:
        return
    engine = create_async_engine(postgres_migrated)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {TABLES}, categories CASCADE"))
        await connection.execute(insert(Category.__table__), seeded_categories)
    await engine.dispose()


@pytest.fixture
async def runtime(
    settings: Settings, push: FakeChannel, sms: FakeChannel
) -> AsyncIterator[Runtime]:
    rt = Runtime.create(settings, channels={"push": push, "sms": sms})
    try:
        yield rt
    finally:
        await rt.aclose()


@pytest.fixture
async def admin_key(runtime: Runtime) -> str:
    async with runtime.transaction() as session:
        _, raw = await api_keys.create(session, "tests", [Scope.ADMIN])
    return raw


@pytest.fixture
async def client(runtime: Runtime, admin_key: str) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(runtime=runtime)
    app.state.runtime = runtime
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://cue.test",
        headers={"Authorization": f"Bearer {admin_key}"},
    ) as http:
        yield http
