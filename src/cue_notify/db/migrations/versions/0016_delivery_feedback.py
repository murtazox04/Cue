from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("addresses") as batch_op:
        batch_op.add_column(
            sa.Column("soft_bounces", sa.Integer(), nullable=False, server_default="0")
        )
    op.create_index("ix_messages_provider_id", "messages", ["channel", "provider_message_id"])


def downgrade() -> None:
    op.drop_index("ix_messages_provider_id", table_name="messages")
    with op.batch_alter_table("addresses") as batch_op:
        batch_op.drop_column("soft_bounces")
