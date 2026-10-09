"""Initial schema and default categories.

Revision ID: 0001
Revises:
Create Date: 2026-10-09 06:02:14.854718
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_usage",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("day", name=op.f("pk_ai_usage")),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "scopes", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("prefix", name=op.f("uq_api_keys_prefix")),
    )
    op.create_table(
        "categories",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "frequency_caps",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("respect_quiet_hours", sa.Boolean(), nullable=False),
        sa.Column("allow_unsubscribe", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_categories")),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column(
            "payload", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("key", sa.String(length=255), nullable=True),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=100), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("dead", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("key", name=op.f("uq_jobs_key")),
    )
    op.create_index("ix_jobs_ready", "jobs", ["dead", "run_at"])
    op.create_table(
        "recipients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("locale", sa.String(length=35), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=True),
        sa.Column(
            "attributes", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column(
            "preferences", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipients")),
        sa.UniqueConstraint("external_id", name=op.f("uq_recipients_external_id")),
    )
    op.create_table(
        "templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("default_locale", sa.String(length=35), nullable=False),
        sa.Column(
            "locales", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("ai_instructions", sa.Text(), nullable=True),
        sa.Column(
            "ai_attributes",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_templates")),
        sa.UniqueConstraint("key", name=op.f("uq_templates_key")),
    )
    op.create_table(
        "addresses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=2048), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("disabled_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["recipients.id"],
            name=op.f("ck_addresses_recipient_id_recipients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_addresses")),
        sa.UniqueConstraint(
            "recipient_id", "channel", "value", name=op.f("uq_addresses_recipient_id_channel_value")
        ),
    )
    op.create_index(op.f("ix_addresses_recipient_id"), "addresses", ["recipient_id"])
    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("template_key", sa.String(length=128), nullable=False),
        sa.Column("category_key", sa.String(length=64), nullable=False),
        sa.Column(
            "channels", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column(
            "audience", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column(
            "variables", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "scheduled",
                "running",
                "paused",
                "completed",
                "cancelled",
                name="broadcaststatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor", sa.String(length=64), nullable=True),
        sa.Column(
            "stats", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_key"],
            ["categories.key"],
            name=op.f("ck_broadcasts_category_key_categories"),
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_key"],
            ["templates.key"],
            name=op.f("ck_broadcasts_template_key_templates"),
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_broadcasts")),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("recipient_id", sa.Uuid(), nullable=True),
        sa.Column("data", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "processed", "failed", name="eventstatus", native_enum=False, length=16
            ),
            nullable=False,
        ),
        sa.Column(
            "outcome", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["recipients.id"],
            name=op.f("ck_events_recipient_id_recipients"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        sa.UniqueConstraint(
            "source", "idempotency_key", name=op.f("uq_events_source_idempotency_key")
        ),
    )
    op.create_index(op.f("ix_events_name"), "events", ["name"])
    op.create_index(op.f("ix_events_recipient_id"), "events", ["recipient_id"])
    op.create_table(
        "rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("event", sa.String(length=255), nullable=False),
        sa.Column(
            "condition", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
        ),
        sa.Column("template_key", sa.String(length=128), nullable=False),
        sa.Column("category_key", sa.String(length=64), nullable=False),
        sa.Column(
            "channels", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("stop_processing", sa.Boolean(), nullable=False),
        sa.Column("delay_seconds", sa.Integer(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=True),
        sa.Column("rollout_percent", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_key"],
            ["categories.key"],
            name=op.f("ck_rules_category_key_categories"),
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["template_key"],
            ["templates.key"],
            name=op.f("ck_rules_template_key_templates"),
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rules")),
        sa.UniqueConstraint("key", name=op.f("uq_rules_key")),
    )
    op.create_index(op.f("ix_rules_event"), "rules", ["event"])
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("rule_id", sa.Uuid(), nullable=True),
        sa.Column("broadcast_id", sa.Uuid(), nullable=True),
        sa.Column("template_key", sa.String(length=128), nullable=True),
        sa.Column("template_version", sa.Integer(), nullable=True),
        sa.Column("category_key", sa.String(length=64), nullable=False),
        sa.Column(
            "channels", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("channel", sa.String(length=64), nullable=True),
        sa.Column("locale", sa.String(length=35), nullable=True),
        sa.Column(
            "content", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("personalize", sa.Boolean(), nullable=False),
        sa.Column("generated_by", sa.String(length=128), nullable=False),
        sa.Column("ai_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "sent",
                "delivered",
                "failed",
                "suppressed",
                "cancelled",
                name="messagestatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("status_reason", sa.String(length=500), nullable=True),
        sa.Column("dedup_key", sa.String(length=255), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("tracking_token", sa.String(length=64), nullable=False),
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("conversion_value", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["broadcast_id"],
            ["broadcasts.id"],
            name=op.f("ck_messages_broadcast_id_broadcasts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
            name=op.f("ck_messages_event_id_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["recipients.id"],
            name=op.f("ck_messages_recipient_id_recipients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rule_id"], ["rules.id"], name=op.f("ck_messages_rule_id_rules"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("dedup_key", name=op.f("uq_messages_dedup_key")),
        sa.UniqueConstraint("tracking_token", name=op.f("uq_messages_tracking_token")),
    )
    op.create_index(op.f("ix_messages_broadcast_id"), "messages", ["broadcast_id"])
    op.create_index("ix_messages_caps", "messages", ["recipient_id", "category_key", "send_at"])
    op.create_index("ix_messages_cooldown", "messages", ["recipient_id", "rule_id", "send_at"])
    op.create_index(op.f("ix_messages_event_id"), "messages", ["event_id"])
    op.create_index(op.f("ix_messages_status"), "messages", ["status"])

    _seed_categories()


def _seed_categories() -> None:
    categories = sa.table(
        "categories",
        sa.column("key", sa.String),
        sa.column("description", sa.Text),
        sa.column("frequency_caps", sa.JSON),
        sa.column("respect_quiet_hours", sa.Boolean),
        sa.column("allow_unsubscribe", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    op.bulk_insert(
        categories,
        [
            {
                "key": "transactional",
                "description": (
                    "Messages a recipient expects as a direct result of their own actions: "
                    "receipts, security codes, status changes. Never capped, never delayed "
                    "by quiet hours, cannot be unsubscribed from."
                ),
                "frequency_caps": [],
                "respect_quiet_hours": False,
                "allow_unsubscribe": False,
                "created_at": now,
                "updated_at": now,
            },
            {
                "key": "marketing",
                "description": "Promotions and announcements. Capped, quiet, opt-out.",
                "frequency_caps": [{"period": "P1D", "limit": 2}, {"period": "P7D", "limit": 5}],
                "respect_quiet_hours": True,
                "allow_unsubscribe": True,
                "created_at": now,
                "updated_at": now,
            },
        ],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_messages_status"), table_name="messages")
    op.drop_index(op.f("ix_messages_event_id"), table_name="messages")
    op.drop_index("ix_messages_cooldown", table_name="messages")
    op.drop_index("ix_messages_caps", table_name="messages")
    op.drop_index(op.f("ix_messages_broadcast_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_rules_event"), table_name="rules")
    op.drop_table("rules")
    op.drop_index(op.f("ix_events_recipient_id"), table_name="events")
    op.drop_index(op.f("ix_events_name"), table_name="events")
    op.drop_table("events")
    op.drop_table("broadcasts")
    op.drop_index(op.f("ix_addresses_recipient_id"), table_name="addresses")
    op.drop_table("addresses")
    op.drop_table("templates")
    op.drop_table("recipients")
    op.drop_index("ix_jobs_ready", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("categories")
    op.drop_table("api_keys")
    op.drop_table("ai_usage")
