"""P17: provider_accounts — per-org encrypted carrier credentials.

Additive. No rows are created; env-var configuration remains the global fallback until
an admin adds an account through the API.

Revision ID: 0018_provider_accounts
Revises: 0017_agent_calls_read
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0018_provider_accounts"
down_revision = "0017_agent_calls_read"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_accounts",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id",
            GUID(),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("label", sa.String(127), nullable=False, server_default=""),
        sa.Column("credentials_encrypted", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="unverified"),
        sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_probe_detail", sa.String(512), nullable=True),
        sa.Column(
            "created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "provider", name="uq_provider_accounts_org_provider"),
    )
    # The webhook verification fallback scans (provider, status) across all orgs.
    op.create_index(
        "ix_provider_accounts_provider_status", "provider_accounts", ["provider", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_provider_accounts_provider_status", table_name="provider_accounts")
    op.drop_table("provider_accounts")
