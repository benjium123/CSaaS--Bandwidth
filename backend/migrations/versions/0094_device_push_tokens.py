"""Ringlite apps P2: native push (FCM) tokens, one per device session.

Additive. ``session_id`` is unique (a device session has at most one token); ``user_id``
is indexed for the fan-out lookup. ``downgrade`` drops the table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0094_device_push_tokens"
down_revision = "0093_device_sessions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "device_push_tokens",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "session_id",
            GUID(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("token", sa.String(4096), nullable=False),
        sa.Column("app_version", sa.String(40), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_device_push_tokens_user_id", "device_push_tokens", ["user_id"])
    op.create_index(
        "ix_device_push_tokens_session_id", "device_push_tokens", ["session_id"], unique=True
    )


def downgrade():
    op.drop_index("ix_device_push_tokens_session_id", table_name="device_push_tokens")
    op.drop_index("ix_device_push_tokens_user_id", table_name="device_push_tokens")
    op.drop_table("device_push_tokens")
