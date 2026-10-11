"""Approval requests: ask a person to decide on something outside Cue.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("reviewers", JSON, nullable=False),
        sa.Column("importance", sa.String(length=16), nullable=False),
        sa.Column("agent_key", sa.String(length=64), nullable=True),
        sa.Column("requested_by", sa.String(length=100), nullable=False),
        sa.Column("metadata", JSON, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(length=300), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_approvals_agent_key", "approvals", ["agent_key"])
    op.create_index("ix_approvals_status", "approvals", ["status"])
    with op.batch_alter_table("review_links") as batch_op:
        batch_op.alter_column("message_id", existing_type=sa.Uuid(), nullable=True)
        batch_op.add_column(sa.Column("approval_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_review_links_approval_id_approvals",
            "approvals",
            ["approval_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_review_links_approval_id", ["approval_id"])


def downgrade() -> None:
    with op.batch_alter_table("review_links") as batch_op:
        batch_op.drop_index("ix_review_links_approval_id")
        batch_op.drop_constraint("fk_review_links_approval_id_approvals", type_="foreignkey")
        batch_op.drop_column("approval_id")
        batch_op.alter_column("message_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_index("ix_approvals_status", table_name="approvals")
    op.drop_index("ix_approvals_agent_key", table_name="approvals")
    op.drop_table("approvals")
