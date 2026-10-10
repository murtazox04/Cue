"""Message importance and expiry.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _importance() -> sa.Enum:
    return sa.Enum(
        "low", "normal", "high", "critical", name="importance", native_enum=False, length=16
    )


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch_op:
        batch_op.add_column(
            sa.Column("importance", _importance(), nullable=False, server_default="normal")
        )
        batch_op.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("rules") as batch_op:
        batch_op.add_column(
            sa.Column("importance", _importance(), nullable=False, server_default="normal")
        )
        batch_op.add_column(sa.Column("expires_after_seconds", sa.Integer(), nullable=True))
    # The default only fills existing rows; the application always sets the value.
    with op.batch_alter_table("messages") as batch_op:
        batch_op.alter_column("importance", server_default=None)
    with op.batch_alter_table("rules") as batch_op:
        batch_op.alter_column("importance", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("rules") as batch_op:
        batch_op.drop_column("expires_after_seconds")
        batch_op.drop_column("importance")
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("expires_at")
        batch_op.drop_column("importance")
