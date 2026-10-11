from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_messages_created_at", "messages", ["created_at"])
    op.create_index("ix_events_received_at", "events", ["received_at"])
    op.create_index("ix_digests_message_id", "digests", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_digests_message_id", table_name="digests")
    op.drop_index("ix_events_received_at", table_name="events")
    op.drop_index("ix_messages_created_at", table_name="messages")
