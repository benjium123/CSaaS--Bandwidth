"""P31 notifications, PWA and mobile: web push subscriptions and per-user preferences.

- push_subscriptions      one row per browser/device subscription (endpoint + keys)
- users.notification_prefs JSON: {mention, assignment, new_inbound, missed_call,
                          sla_breach: bool, digest: bool} - five event toggles + a digest switch

Additive. Revision chains after 0032_reports.

Revision ID: 0033_notifications_push
Revises: 0032_reports
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0033_notifications_push"
down_revision = "0032_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint", sa.String(1024), nullable=False),
        sa.Column("keys", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "endpoint", name="uq_push_subscriptions_user_endpoint"),
    )
    op.create_index("ix_push_subscriptions_org_user", "push_subscriptions", ["org_id", "user_id"])

    op.add_column("users", sa.Column("notification_prefs", PortableJSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "notification_prefs")
    op.drop_index("ix_push_subscriptions_org_user", table_name="push_subscriptions")
    op.drop_table("push_subscriptions")
