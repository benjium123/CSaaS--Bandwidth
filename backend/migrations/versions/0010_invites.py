"""invites: invite-only registration

Additive - one new table.

Revision ID: 0010_invites
Revises: 0009_appointments_kb
Create Date: 2026-08-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0010_invites"
down_revision = "0009_appointments_kb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invites",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("email", sa.String(320), nullable=False, index=True),
        sa.Column("role_name", sa.String(32), nullable=False, server_default="agent"),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("token_hash", name="uq_invites_token_hash"),
    )
    op.create_index("ix_invites_token_hash", "invites", ["token_hash"])
    op.create_index("ix_invites_org_email", "invites", ["org_id", "email"])


def downgrade() -> None:
    op.drop_index("ix_invites_org_email", table_name="invites")
    op.drop_index("ix_invites_token_hash", table_name="invites")
    op.drop_table("invites")
