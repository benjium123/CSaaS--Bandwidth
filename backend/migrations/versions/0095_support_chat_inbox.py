"""Support chat inbox: customer chats carry their workspace/user, operators can assign a chat
and have read state.

Additive and nullable, like 0092: org_id/user_id are NULL for anonymous website visitors,
assigned_user_id NULL = unassigned, agent_read_at NULL = never opened by an operator,
last_visitor_at NULL = the visitor has not written since the handoff.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0095_support_chat_inbox"
down_revision = "0094_device_push_tokens"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("site_chats", sa.Column("org_id", GUID(), nullable=True))
    op.add_column("site_chats", sa.Column("user_id", GUID(), nullable=True))
    op.add_column("site_chats", sa.Column("assigned_user_id", GUID(), nullable=True))
    op.add_column(
        "site_chats", sa.Column("agent_read_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "site_chats", sa.Column("last_visitor_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_site_chats_org_id", "site_chats", ["org_id"])
    op.create_index("ix_site_chats_assigned_user_id", "site_chats", ["assigned_user_id"])


def downgrade():
    op.drop_index("ix_site_chats_assigned_user_id", table_name="site_chats")
    op.drop_index("ix_site_chats_org_id", table_name="site_chats")
    op.drop_column("site_chats", "last_visitor_at")
    op.drop_column("site_chats", "agent_read_at")
    op.drop_column("site_chats", "assigned_user_id")
    op.drop_column("site_chats", "user_id")
    op.drop_column("site_chats", "org_id")
