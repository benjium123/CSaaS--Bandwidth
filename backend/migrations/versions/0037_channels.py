"""P35 channels: email, WhatsApp, web chat in the same inbox.

- message_threads.channel   'sms' | 'call' | 'email' | 'whatsapp' | 'webchat' (backfilled 'sms')
- channel_accounts          per-org connections (email smtp/imap/postmark, whatsapp meta/twilio)
- messages.subject, messages.html_body   email fields
- webchat_widgets           embeddable widget keys, allowed origins, theme

Additive. Revision chains after 0036_integrations.

Revision ID: 0037_channels
Revises: 0036_integrations
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0037_channels"
down_revision = "0036_integrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "message_threads",
        sa.Column("channel", sa.String(8), nullable=False, server_default="sms"),
    )
    op.create_index("ix_threads_org_channel", "message_threads", ["org_id", "channel"])

    op.create_table(
        "channel_accounts",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        # email | whatsapp
        sa.Column("kind", sa.String(8), nullable=False),
        # email: smtp_imap | postmark ; whatsapp: meta | twilio
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("label", sa.String(127), nullable=False, server_default=""),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column("config", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="unverified"),
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_probe_detail", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "kind", "label", name="uq_channel_accounts_org_kind_label"),
    )
    op.create_index("ix_channel_accounts_org_kind", "channel_accounts", ["org_id", "kind"])

    op.add_column("messages", sa.Column("subject", sa.String(255), nullable=True))
    op.add_column("messages", sa.Column("html_body", sa.Text(), nullable=True))

    op.create_table(
        "webchat_widgets",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(32), nullable=False),
        sa.Column("name", sa.String(63), nullable=False, server_default="Website chat"),
        sa.Column("allowed_origins", PortableJSON(), nullable=False, server_default="[]"),
        sa.Column("theme", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column(
            "inbox_id", GUID(), sa.ForeignKey("inboxes.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("key", name="uq_webchat_widgets_key"),
    )
    op.create_index("ix_webchat_widgets_org", "webchat_widgets", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_webchat_widgets_org", table_name="webchat_widgets")
    op.drop_table("webchat_widgets")
    op.drop_column("messages", "html_body")
    op.drop_column("messages", "subject")
    op.drop_index("ix_channel_accounts_org_kind", table_name="channel_accounts")
    op.drop_table("channel_accounts")
    op.drop_index("ix_threads_org_channel", table_name="message_threads")
    op.drop_column("message_threads", "channel")
