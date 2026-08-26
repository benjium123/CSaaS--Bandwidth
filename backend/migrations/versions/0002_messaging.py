"""messaging: numbers, threads, messages, event ledger, dead letters

Revision ID: 0002_messaging
Revises: 0001_foundation
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0002_messaging"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_numbers",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("e164", sa.String(20), nullable=False),
        sa.Column("carrier", sa.String(16), nullable=False, server_default="bandwidth"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_org_numbers_org_id", "org_numbers", ["org_id"])
    # Globally unique: a number belongs to exactly one org, ever.
    op.create_index("ix_org_numbers_e164", "org_numbers", ["e164"], unique=True)

    op.create_table(
        "message_threads",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("our_e164", sa.String(20), nullable=False),
        sa.Column("contact_e164", sa.String(20), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "our_e164", "contact_e164", name="uq_threads_org_pair"),
    )
    op.create_index("ix_message_threads_org_id", "message_threads", ["org_id"])

    op.create_table(
        "messages",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "thread_id",
            GUID(),
            sa.ForeignKey("message_threads.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("from_e164", sa.String(20), nullable=False),
        sa.Column("to_e164", sa.String(20), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("media", PortableJSON(), nullable=False),
        sa.Column("carrier", sa.String(16), nullable=False, server_default="bandwidth"),
        sa.Column("provider_message_id", sa.String(64), nullable=True),
        sa.Column("segment_count_est", sa.Integer(), nullable=True),
        sa.Column("segment_count_carrier", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column("error_detail", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("carrier", "provider_message_id", name="uq_messages_provider_id"),
    )
    op.create_index("ix_messages_org_id", "messages", ["org_id"])
    op.create_index(
        "ix_messages_org_thread_created", "messages", ["org_id", "thread_id", "created_at"]
    )

    op.create_table(
        "message_events",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "message_id", GUID(), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("carrier", sa.String(16), nullable=False),
        sa.Column("provider_message_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload", PortableJSON(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_error", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # THIS CONSTRAINT IS THE IDEMPOTENCY MECHANISM. Bandwidth publishes no event id
        # and retries in parallel, so only a DB constraint is safe.
        sa.UniqueConstraint(
            "carrier", "provider_message_id", "event_type", name="uq_msg_events_dedupe"
        ),
    )
    op.create_index("ix_message_events_org_id", "message_events", ["org_id"])
    op.create_index("ix_message_events_message_id", "message_events", ["message_id"])

    op.create_table(
        "webhook_dead_letters",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("carrier", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("webhook_dead_letters")
    op.drop_table("message_events")
    op.drop_table("messages")
    op.drop_table("message_threads")
    op.drop_table("org_numbers")
