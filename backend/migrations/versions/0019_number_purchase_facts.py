"""P18: purchase facts + provider-account linkage on org_numbers.

Additive, all nullable. Existing numbers keep NULLs (hand-added / env carriers).

Revision ID: 0019_number_purchase_facts
Revises: 0018_provider_accounts
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0019_number_purchase_facts"
down_revision = "0018_provider_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "org_numbers",
        sa.Column(
            "provider_account_id",
            GUID(),
            sa.ForeignKey("provider_accounts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("org_numbers", sa.Column("purchase_cost_cents", sa.Integer(), nullable=True))
    op.add_column("org_numbers", sa.Column("monthly_cost_cents", sa.Integer(), nullable=True))
    op.add_column(
        "org_numbers", sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("org_numbers", sa.Column("order_detail", sa.String(512), nullable=True))
    op.create_index("ix_org_numbers_status", "org_numbers", ["status"])


def downgrade() -> None:
    op.drop_index("ix_org_numbers_status", table_name="org_numbers")
    op.drop_column("org_numbers", "order_detail")
    op.drop_column("org_numbers", "purchased_at")
    op.drop_column("org_numbers", "monthly_cost_cents")
    op.drop_column("org_numbers", "purchase_cost_cents")
    op.drop_column("org_numbers", "provider_account_id")
