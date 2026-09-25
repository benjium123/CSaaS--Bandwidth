"""Public website: sales enquiries and chat-assistant handoffs. Additive only."""

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0073_site_chat"
down_revision = "0072_workspace_plans"
branch_labels = None
depends_on = None


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade():
    op.create_table(
        "site_leads",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("phone", sa.String(40)),
        sa.Column("company", sa.String(160)),
        sa.Column("team_size", sa.String(20), nullable=False),
        sa.Column("numbers_needed", sa.String(20), nullable=False),
        sa.Column("switching_from", sa.String(60)),
        sa.Column("message", sa.Text()),
        sa.Column("sms_consent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("plan", sa.String(32)),
        sa.Column("page", sa.String(200)),
        sa.Column("ip", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False, server_default="new", index=True),
        *_timestamps(),
    )
    op.create_table(
        "site_chats",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="waiting", index=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("phone", sa.String(40)),
        sa.Column("sms_consent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.String(60)),
        sa.Column("page", sa.String(200)),
        sa.Column("ip", sa.String(64)),
        sa.Column("agent_name", sa.String(120)),
        sa.Column("last_message_at", sa.DateTime(timezone=True), index=True),
        *_timestamps(),
    )
    op.create_table(
        "site_chat_messages",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "chat_id", GUID(), sa.ForeignKey("site_chats.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_site_chat_messages_chat_created", "site_chat_messages", ["chat_id", "created_at"]
    )


def downgrade():
    op.drop_index("ix_site_chat_messages_chat_created", table_name="site_chat_messages")
    op.drop_table("site_chat_messages")
    op.drop_table("site_chats")
    op.drop_table("site_leads")
