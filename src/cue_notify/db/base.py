"""Declarative base, portable column types and shared mixins."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, DateTime, Dialect, MetaData, TypeDecorator
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from cue_notify.core.ids import uuid7
from cue_notify.core.time import utcnow

# Deterministic constraint names keep Alembic migrations stable across databases.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on every backend.

    PostgreSQL stores ``timestamptz`` natively. SQLite has no time zone support, so values
    are stored as naive UTC and re-attached to UTC on load. Either way Python code only
    ever sees aware datetimes.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are not allowed; use cue_notify.core.time.utcnow()")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


JSONType = JSON().with_variant(JSONB(), "postgresql")


def _enum_values(members: type[StrEnum]) -> list[str]:
    return [member.value for member in members]


def enum_type(enum_cls: type[StrEnum]) -> SAEnum:
    """Store a ``StrEnum`` as its value in a VARCHAR (no native DB enum to migrate)."""
    return SAEnum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=16,
        values_callable=_enum_values,
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {  # noqa: RUF012 — SQLAlchemy reads this class attribute
        datetime: UTCDateTime(),
        dict[str, Any]: JSONType,
        list[str]: JSONType,
        list[dict[str, Any]]: JSONType,
    }


class IdMixin:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid7, sort_order=-10)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow, sort_order=90)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, sort_order=91)
