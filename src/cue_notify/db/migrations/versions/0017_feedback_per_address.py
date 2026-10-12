from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column("address", sa.String(length=2048), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("bounced", sa.Boolean(), nullable=False),
        sa.Column("soft_bounced", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name="fk_message_deliveries_message_id_messages",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_message_deliveries"),
    )
    op.create_index("ix_message_deliveries_message_id", "message_deliveries", ["message_id"])
    op.create_index(
        "ix_message_deliveries_provider_id",
        "message_deliveries",
        ["channel", "provider_message_id"],
    )
    op.create_table(
        "feedback_reports",
        sa.Column("report_id", sa.String(length=255), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("report_id", name="pk_feedback_reports"),
    )
    op.create_index("ix_feedback_reports_received_at", "feedback_reports", ["received_at"])
    with op.batch_alter_table("addresses") as batch_op:
        batch_op.add_column(sa.Column("complained_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("addresses") as batch_op:
        batch_op.drop_column("complained_at")
    op.drop_index("ix_feedback_reports_received_at", table_name="feedback_reports")
    op.drop_table("feedback_reports")
    op.drop_index("ix_message_deliveries_provider_id", table_name="message_deliveries")
    op.drop_index("ix_message_deliveries_message_id", table_name="message_deliveries")
    op.drop_table("message_deliveries")
