"""Agents: automated senders with attention budgets and approvals.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("categories", JSON, nullable=True),
        sa.Column(
            "max_importance",
            sa.Enum(
                "low", "normal", "high", "critical", name="importance", native_enum=False, length=16
            ),
            nullable=False,
        ),
        sa.Column("frequency_caps", JSON, nullable=False),
        sa.Column("approval", JSON, nullable=True),
        sa.Column("approval_ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_agents")),
    )
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.add_column(sa.Column("agent_key", sa.String(length=64), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_api_keys_agent_key_agents"),
            "agents",
            ["agent_key"],
            ["key"],
            ondelete="RESTRICT",
            onupdate="CASCADE",
        )
    with op.batch_alter_table("messages") as batch_op:
        batch_op.add_column(sa.Column("agent_key", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("reviewed_by", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_messages_agent", "messages", ["recipient_id", "agent_key", "send_at"])


def downgrade() -> None:
    op.drop_index("ix_messages_agent", table_name="messages")
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("reviewed_at")
        batch_op.drop_column("reviewed_by")
        batch_op.drop_column("agent_key")
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.drop_constraint(op.f("fk_api_keys_agent_key_agents"), type_="foreignkey")
        batch_op.drop_column("agent_key")
    op.drop_table("agents")
