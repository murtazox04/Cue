"""The migration chain must produce exactly the schema the models describe."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from cue_notify.db.base import Base
from cue_notify.db.migrations import alembic_config


def test_upgrade_matches_models_and_downgrade_is_clean(tmp_path: Path) -> None:
    path = tmp_path / "migrations.db"
    config = alembic_config(f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": False})
        assert compare_metadata(context, Base.metadata) == []
        categories = connection.exec_driver_sql("SELECT key FROM categories ORDER BY key").all()
        assert [row[0] for row in categories] == ["marketing", "transactional"]

    command.downgrade(config, "base")
    with engine.connect() as connection:
        assert inspect(connection).get_table_names() == ["alembic_version"]
    engine.dispose()
