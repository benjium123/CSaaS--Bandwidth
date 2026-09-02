"""P19: provider rate overrides + derived daily spend.

Additive. No rows are created; defaults live in code until an operator overrides.

Revision ID: 0020_provider_spend
Revises: 0019_number_purchase_facts
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0020_provider_spend"
down_revision = "0019_number_purchase_facts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_rates",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("metric", sa.String(16), nullable=False),
        sa.Column("unit_cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "provider", "metric", name="uq_provider_rates_org_provider_metric"),
    )
    op.create_table(
        "provider_spend_daily",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("period_date", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("metric", sa.String(16), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("number_id", GUID(), sa.ForeignKey("org_numbers.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "period_date", "provider", "metric", "number_id", name="uq_provider_spend_daily_key"),
    )
    op.create_index("ix_provider_spend_daily_org_date", "provider_spend_daily", ["org_id", "period_date"])


def downgrade() -> None:
    op.drop_index("ix_provider_spend_daily_org_date", table_name="provider_spend_daily")
    op.drop_table("provider_spend_daily")
    op.drop_table("provider_rates")
