"""Digests: group bursts of events into one message.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
OPEN = sa.text("status = 'open'")


def upgrade() -> None:
    op.create_table(
        "digests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("open", "sent", "cancelled", name="digeststatus", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("items", JSON, nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("closes_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_digests_message_id_messages"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["recipients.id"],
            name=op.f("fk_digests_recipient_id_recipients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["rules.id"], name=op.f("fk_digests_rule_id_rules"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_digests")),
    )
    op.create_index(op.f("ix_digests_recipient_id"), "digests", ["recipient_id"])
    op.create_index(
        "uq_digests_open",
        "digests",
        ["rule_id", "recipient_id", "key"],
        unique=True,
        postgresql_where=OPEN,
        sqlite_where=OPEN,
    )
    op.add_column("messages", sa.Column("ai_context", JSON, nullable=True))
    op.add_column("rules", sa.Column("digest", JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("rules") as batch_op:
        batch_op.drop_column("digest")
    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_column("ai_context")
    op.drop_index("uq_digests_open", table_name="digests")
    op.drop_index(op.f("ix_digests_recipient_id"), table_name="digests")
    op.drop_table("digests")
