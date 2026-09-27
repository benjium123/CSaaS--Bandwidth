"""Per-workspace discounts (org_discounts) + the discount carried by each usage charge.

One row per (org, category): a percentage in basis points, an optional end, and the Stripe
coupon that carries it on subscriptions. ``credit_ledger.discount_micros`` records what a
usage charge was discounted by, so the console can show profit before and after discounts.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0085_org_discounts"
down_revision = "0080_recovery_codes_ack"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "org_discounts",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("percent_bps", sa.Integer(), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("stripe_coupon_id", sa.String(64), nullable=True),
        sa.Column("updated_by", GUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "category", name="uq_org_discounts_category"),
        sa.CheckConstraint(
            "percent_bps > 0 AND percent_bps <= 10000", name="ck_org_discounts_percent"
        ),
    )
    op.create_index("ix_org_discounts_org_id", "org_discounts", ["org_id"])
    with op.batch_alter_table("credit_ledger") as batch:
        batch.add_column(
            sa.Column("discount_micros", sa.BigInteger(), nullable=False, server_default="0")
        )


def downgrade():
    with op.batch_alter_table("credit_ledger") as batch:
        batch.drop_column("discount_micros")
    op.drop_index("ix_org_discounts_org_id", table_name="org_discounts")
    op.drop_table("org_discounts")
