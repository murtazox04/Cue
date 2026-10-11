"""Hosting platforms hand out DATABASE_URL and PORT; Cue should just work there."""

from __future__ import annotations

import pytest

from cue_notify.settings import DatabaseSettings, Settings


def test_plain_postgres_urls_get_the_async_driver() -> None:
    for url in ("postgres://u:p@db:5432/cue", "postgresql://u:p@db:5432/cue"):
        assert DatabaseSettings(url=url).url == "postgresql+asyncpg://u:p@db:5432/cue"
    sqlite = "sqlite+aiosqlite:///./cue.db"
    assert DatabaseSettings(url=sqlite).url == sqlite


def test_database_url_is_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@db/cue")
    monkeypatch.delenv("CUE_DATABASE__URL", raising=False)
    assert Settings().database.url == "postgresql+asyncpg://u:p@db/cue"
    monkeypatch.setenv("CUE_DATABASE__URL", "sqlite+aiosqlite:///./other.db")
    assert Settings().database.url == "sqlite+aiosqlite:///./other.db"
