"""P26 inbox pro: private notes + mentions, snooze, SLA timers, notifications.

- thread_notes                          private notes on a conversation (never sent), with
                                        @mentions (user ids) that fan out to notifications
- message_threads.snoozed_until,
  first_response_at, sla_breached_at    snooze + SLA bookkeeping per conversation
- inboxes.sla_first_response_minutes,
  sla_resolution_minutes                per-inbox targets (NULL = no SLA)
- notifications                         per-user in-app notifications (bell menu)

Additive. Revision chains after 0027_enterprise_identity.

Revision ID: 0028_inbox_pro
Revises: 0027_enterprise_identity
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0028_inbox_pro"
down_revision = "0027_enterprise_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "thread_notes",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "thread_id",
            GUID(),
            sa.ForeignKey("message_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "author_user_id", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("mentions", PortableJSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_thread_notes_org_thread", "thread_notes", ["org_id", "thread_id"])

    op.add_column(
        "message_threads", sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "message_threads",
        sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "message_threads", sa.Column("sla_breached_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_threads_org_snoozed", "message_threads", ["org_id", "snoozed_until"])

    op.add_column(
        "inboxes", sa.Column("sla_first_response_minutes", sa.Integer(), nullable=True)
    )
    op.add_column("inboxes", sa.Column("sla_resolution_minutes", sa.Integer(), nullable=True))

    op.create_table(
        "notifications",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        # mention | assignment | overdue | missed_call | new_inbound | low_balance
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "thread_id",
            GUID(),
            sa.ForeignKey("message_threads.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("body", sa.String(255), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedupe_key", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "user_id", "dedupe_key", name="uq_notifications_dedupe"),
    )
    op.create_index(
        "ix_notifications_user_unread", "notifications", ["org_id", "user_id", "read_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_table("notifications")
    op.drop_column("inboxes", "sla_resolution_minutes")
    op.drop_column("inboxes", "sla_first_response_minutes")
    op.drop_index("ix_threads_org_snoozed", table_name="message_threads")
    op.drop_column("message_threads", "sla_breached_at")
    op.drop_column("message_threads", "first_response_at")
    op.drop_column("message_threads", "snoozed_until")
    op.drop_index("ix_thread_notes_org_thread", table_name="thread_notes")
    op.drop_table("thread_notes")
