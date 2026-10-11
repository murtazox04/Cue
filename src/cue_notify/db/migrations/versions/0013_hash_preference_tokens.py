import hashlib
import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BATCH_SIZE = 1000


def recipients_table() -> sa.TableClause:
    return sa.table(
        "recipients",
        sa.column("id", sa.Uuid()),
        sa.column("preference_token", sa.String()),
        sa.column("preference_token_id", sa.String()),
        sa.column("preference_token_hash", sa.String()),
    )


def upgrade() -> None:
    with op.batch_alter_table("recipients") as batch_op:
        batch_op.add_column(sa.Column("preference_token_id", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("preference_token_hash", sa.String(length=64), nullable=True))
    recipients = recipients_table()
    connection = op.get_bind()
    last_id = None
    while True:
        query = sa.select(recipients.c.id, recipients.c.preference_token).order_by(recipients.c.id)
        if last_id is not None:
            query = query.where(recipients.c.id > last_id)
        rows = connection.execute(query.limit(BATCH_SIZE)).all()
        if not rows:
            break
        connection.execute(
            recipients.update()
            .where(recipients.c.id == sa.bindparam("row_id"))
            .values(
                preference_token_id=sa.bindparam("token_id"),
                preference_token_hash=sa.bindparam("token_hash"),
            ),
            [
                {
                    "row_id": row_id,
                    "token_id": secrets.token_hex(12),
                    "token_hash": hashlib.sha256(plaintext.encode()).hexdigest(),
                }
                for row_id, plaintext in rows
            ],
        )
        last_id = rows[-1][0]
    with op.batch_alter_table("recipients") as batch_op:
        batch_op.drop_constraint(op.f("uq_recipients_preference_token"), type_="unique")
        batch_op.drop_column("preference_token")
        batch_op.alter_column("preference_token_id", existing_type=sa.String(32), nullable=False)
        batch_op.create_unique_constraint(
            op.f("uq_recipients_preference_token_id"), ["preference_token_id"]
        )
        batch_op.create_unique_constraint(
            op.f("uq_recipients_preference_token_hash"), ["preference_token_hash"]
        )


def downgrade() -> None:
    with op.batch_alter_table("recipients") as batch_op:
        batch_op.add_column(sa.Column("preference_token", sa.String(length=64), nullable=True))
    recipients = recipients_table()
    connection = op.get_bind()
    for (row_id,) in connection.execute(sa.select(recipients.c.id)).all():
        connection.execute(
            recipients.update()
            .where(recipients.c.id == row_id)
            .values(preference_token=secrets.token_urlsafe(24))
        )
    with op.batch_alter_table("recipients") as batch_op:
        batch_op.drop_constraint(op.f("uq_recipients_preference_token_hash"), type_="unique")
        batch_op.drop_constraint(op.f("uq_recipients_preference_token_id"), type_="unique")
        batch_op.drop_column("preference_token_hash")
        batch_op.drop_column("preference_token_id")
        batch_op.alter_column("preference_token", existing_type=sa.String(64), nullable=False)
        batch_op.create_unique_constraint(
            op.f("uq_recipients_preference_token"), ["preference_token"]
        )
