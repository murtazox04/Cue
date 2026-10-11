"""One-time review links for messages waiting for approval.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_links",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_review_links_message_id", "review_links", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_review_links_message_id", table_name="review_links")
    op.drop_table("review_links")
