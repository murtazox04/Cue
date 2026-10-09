"""Alembic migrations, shipped inside the package so ``cuectl db upgrade`` works anywhere."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

SCRIPT_LOCATION = Path(__file__).parent


def alembic_config(url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(SCRIPT_LOCATION))
    if url:
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config
