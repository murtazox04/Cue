"""Preference centre: per-recipient preference tokens and the consent ledger.

Revision ID: 0003
Revises: 0002
"""

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "preference_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "api",
                "self_service",
                "one_click",
                name="preferencesource",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("changes", JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["recipients.id"],
            name=op.f("fk_preference_changes_recipient_id_recipients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_preference_changes")),
    )
    op.create_index(
        op.f("ix_preference_changes_recipient_id"), "preference_changes", ["recipient_id"]
    )

    op.add_column("recipients", sa.Column("preference_token", sa.String(length=64), nullable=True))
    recipients = sa.table("recipients", sa.column("id", sa.Uuid()), sa.column("preference_token"))
    connection = op.get_bind()
    for (recipient_id,) in connection.execute(sa.select(recipients.c.id)).all():
        connection.execute(
            recipients.update()
            .where(recipients.c.id == recipient_id)
            .values(preference_token=secrets.token_urlsafe(24))
        )
    with op.batch_alter_table("recipients") as batch_op:
        batch_op.alter_column("preference_token", existing_type=sa.String(64), nullable=False)
        batch_op.create_unique_constraint(
            op.f("uq_recipients_preference_token"), ["preference_token"]
        )


def downgrade() -> None:
    with op.batch_alter_table("recipients") as batch_op:
        batch_op.drop_constraint(op.f("uq_recipients_preference_token"), type_="unique")
        batch_op.drop_column("preference_token")
    op.drop_index(op.f("ix_preference_changes_recipient_id"), table_name="preference_changes")
    op.drop_table("preference_changes")
