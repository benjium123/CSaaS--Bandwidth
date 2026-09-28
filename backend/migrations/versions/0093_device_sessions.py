"""Ringlite apps P1: device sessions (Android/desktop) and one-time device link codes.

Device sessions are ordinary `sessions` rows with a device_kind, so every existing revocation
path (member removal, disable, password reset, sign-out-everywhere, SCIM, suspension) covers
them. Additive and nullable: existing rows read as web sessions.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0093_device_sessions"
down_revision = "0092_member_reports_to"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("device_kind", sa.String(16), nullable=True))
    op.add_column("sessions", sa.Column("device_name", sa.String(120), nullable=True))
    op.add_column("sessions", sa.Column("device_os", sa.String(60), nullable=True))
    op.add_column("sessions", sa.Column("app_version", sa.String(40), nullable=True))
    op.add_column("sessions", sa.Column("refresh_hash", sa.String(64), nullable=True))
    op.add_column("sessions", sa.Column("refresh_generation", sa.Integer(), nullable=True))

    op.create_table(
        "device_link_codes",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True),
        sa.Column(
            "created_by_sid",
            GUID(),
            sa.ForeignKey("sessions.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_device_link_codes_user_id", "device_link_codes", ["user_id"])


def downgrade():
    op.drop_index("ix_device_link_codes_user_id", table_name="device_link_codes")
    op.drop_table("device_link_codes")
    for col in (
        "refresh_generation",
        "refresh_hash",
        "app_version",
        "device_os",
        "device_name",
        "device_kind",
    ):
        op.drop_column("sessions", col)
