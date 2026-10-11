"""Compliance: marketing categories, recipient regions and consent provenance.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("categories") as batch_op:
        batch_op.add_column(
            sa.Column("marketing", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    with op.batch_alter_table("categories") as batch_op:
        batch_op.alter_column("marketing", server_default=None)
    op.execute("UPDATE categories SET marketing = TRUE WHERE key = 'marketing'")
    with op.batch_alter_table("recipients") as batch_op:
        batch_op.add_column(sa.Column("region", sa.String(length=8), nullable=True))
    with op.batch_alter_table("preference_changes") as batch_op:
        batch_op.add_column(
            sa.Column(
                "provenance",
                sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("preference_changes") as batch_op:
        batch_op.drop_column("provenance")
    with op.batch_alter_table("recipients") as batch_op:
        batch_op.drop_column("region")
    with op.batch_alter_table("categories") as batch_op:
        batch_op.drop_column("marketing")
