"""Create the admin_invites table.

Revision ID: 0060_admin_invites
Revises: 0059_open_registration_kyc_required
Create Date: 2026-09-21

Platform-scoped, single-use invitations to become a platform operator. Only the SHA-256
hex digest of the token is stored; the plaintext lives solely in the URL the trusted CLI
prints. Mirrors app/models/admin_invite.py.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0060_admin_invites"
down_revision = "0059_open_registration_kyc_required"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_invites",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by", GUID(), nullable=True),
        sa.Column(
            "issued_via",
            sa.String(length=32),
            nullable=False,
            server_default="trusted_cli",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["consumed_by"],
            ["users.id"],
            name="fk_admin_invites_consumed_by_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_admin_invites"),
        sa.UniqueConstraint("token_hash", name="uq_admin_invites_token_hash"),
    )
    op.create_index("ix_admin_invites_email", "admin_invites", ["email"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_admin_invites_email", table_name="admin_invites")
    op.drop_table("admin_invites")
