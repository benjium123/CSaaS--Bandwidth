"""P28 messaging completeness: link tracking, send-later, plain failure reasons.

- short_links / link_clicks              tracked links (PUBLIC_WEB_URL/l/{code}) and clicks
- messages.scheduled_for                 single-message send-later (distinct from campaigns)
- messages.failure_reason_public         plain-words failure shown on the bubble

Additive. Revision chains after 0029_contacts_pro.

Revision ID: 0030_messaging_completeness
Revises: 0029_contacts_pro
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0030_messaging_completeness"
down_revision = "0029_contacts_pro"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "short_links",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("target_url", sa.String(2048), nullable=False),
        sa.Column(
            "message_id", GUID(), sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "contact_id", GUID(), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("code", name="uq_short_links_code"),
    )
    op.create_index("ix_short_links_org_message", "short_links", ["org_id", "message_id"])

    op.create_table(
        "link_clicks",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "short_link_id",
            GUID(),
            sa.ForeignKey("short_links.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip_hash", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_link_clicks_link_at", "link_clicks", ["short_link_id", "at"])

    op.add_column(
        "messages", sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_messages_org_scheduled", "messages", ["org_id", "scheduled_for"])
    op.add_column(
        "messages", sa.Column("failure_reason_public", sa.String(255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("messages", "failure_reason_public")
    op.drop_index("ix_messages_org_scheduled", table_name="messages")
    op.drop_column("messages", "scheduled_for")
    op.drop_index("ix_link_clicks_link_at", table_name="link_clicks")
    op.drop_table("link_clicks")
    op.drop_index("ix_short_links_org_message", table_name="short_links")
    op.drop_table("short_links")
