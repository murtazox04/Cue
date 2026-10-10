"""The migration chain must produce exactly the schema the models describe."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

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


def test_preference_tokens_are_backfilled_for_existing_recipients(tmp_path: Path) -> None:
    path = tmp_path / "backfill.db"
    config = alembic_config(f"sqlite+aiosqlite:///{path}")
    command.upgrade(config, "0002")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO recipients (id, external_id, attributes, preferences, created_at, "
                "updated_at) VALUES (:id, :external_id, '{}', '{}', :at, :at)"
            ),
            [
                {"id": f"{n:032x}", "external_id": f"user-{n}", "at": "2026-01-01 00:00:00"}
                for n in range(3)
            ],
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        tokens = [
            row[0] for row in connection.exec_driver_sql("SELECT preference_token FROM recipients")
        ]
    engine.dispose()
    assert len(tokens) == len(set(tokens)) == 3
    assert all(token and len(token) >= 32 for token in tokens)
