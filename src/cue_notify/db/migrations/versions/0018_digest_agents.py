from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OPEN = sa.text("status = 'open'")
OPEN_WITHOUT_AGENT = sa.text("status = 'open' AND agent_key IS NULL")
OPEN_WITH_AGENT = sa.text("status = 'open' AND agent_key IS NOT NULL")
GROUP = ["rule_id", "recipient_id", "key"]


def upgrade() -> None:
    op.add_column("digests", sa.Column("agent_key", sa.String(length=64), nullable=True))
    op.drop_index("uq_digests_open", table_name="digests")
    op.create_index(
        "uq_digests_open",
        "digests",
        GROUP,
        unique=True,
        postgresql_where=OPEN_WITHOUT_AGENT,
        sqlite_where=OPEN_WITHOUT_AGENT,
    )
    op.create_index(
        "uq_digests_open_agent",
        "digests",
        [*GROUP, "agent_key"],
        unique=True,
        postgresql_where=OPEN_WITH_AGENT,
        sqlite_where=OPEN_WITH_AGENT,
    )


def downgrade() -> None:
    op.drop_index("uq_digests_open_agent", table_name="digests")
    op.drop_index("uq_digests_open", table_name="digests")
    op.create_index(
        "uq_digests_open",
        "digests",
        GROUP,
        unique=True,
        postgresql_where=OPEN,
        sqlite_where=OPEN,
    )
    with op.batch_alter_table("digests") as batch_op:
        batch_op.drop_column("agent_key")
