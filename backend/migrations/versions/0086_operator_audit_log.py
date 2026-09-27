"""H1 operator audit log: who on the platform team changed what, when, and why."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0086_operator_audit_log"
down_revision = "0085_org_discounts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "operator_audit_log",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "operator_user_id",
            GUID(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("operator_email", sa.String(320), nullable=True),
        sa.Column("operator_role", sa.String(16), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("route", sa.String(255), nullable=False),
        sa.Column("path_params", PortableJSON(), nullable=False),
        sa.Column("org_id", GUID(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
    )
    op.create_index("ix_operator_audit_log_at", "operator_audit_log", ["at"])
    op.create_index(
        "ix_operator_audit_log_operator", "operator_audit_log", ["operator_user_id", "at"]
    )
    op.create_index("ix_operator_audit_log_org", "operator_audit_log", ["org_id", "at"])


def downgrade():
    op.drop_table("operator_audit_log")
