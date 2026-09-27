"""H3 view as workspace: time-boxed, read-only operator views of a customer workspace."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0088_operator_view_sessions"
down_revision = "0086_operator_audit_log"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operator_view_sessions",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "operator_user_id",
            GUID(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_operator_view_sessions_operator", "operator_view_sessions", ["operator_user_id"]
    )
    op.create_index("ix_operator_view_sessions_org_id", "operator_view_sessions", ["org_id"])


def downgrade():
    op.drop_table("operator_view_sessions")
