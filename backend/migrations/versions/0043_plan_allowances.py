"""P37c packages: per-period plan allowance counters.

- plan_allowances   one row per (org, billing period, metric), holding how many units the
                    org's package includes and how many it has used. Usage takes from this
                    FIRST; whatever does not fit becomes overage, priced from the plan's
                    overage_rates and charged against prepaid credits by
                    services/telephony_billing.py. The unique constraint is what makes the
                    take atomic: one UPDATE ... WHERE used + n <= included per unit batch,
                    so two concurrent sends can never both take the last text.

No money is stored here and no ledger row is written here - credit_ledger keeps exactly one
writer (services/credits.py). Counters only.

Additive and inert on arrival: an org with no plan_code has no rows and every allowance
lookup returns "nothing included", which is precisely today's behaviour (pure prepaid).
Plans themselves are seeded by platform ops, not by this migration - prices are the
operator's to set.

Revision ID: 0043_plan_allowances
Revises: 0042_managed_telephony
Create Date: 2026-09-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0043_plan_allowances"
down_revision = "0042_managed_telephony"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_allowances",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        #: First day of the org's billing period - its plan_started_at ANNIVERSARY, not the
        #: calendar month, so a mid-month signup is not handed a short first period.
        sa.Column("period_start", sa.Date(), nullable=False),
        #: sms_segments | voice_minutes | numbers
        sa.Column("metric", sa.String(32), nullable=False),
        #: Snapshotted from the plan when the period opens. Kept per period rather than read
        #: live from plans.included so that changing a plan's allowance never retroactively
        #: rewrites what an org was already given this month.
        sa.Column("included_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("used_units", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "org_id", "period_start", "metric", name="uq_plan_allowances_org_period_metric"
        ),
    )
    op.create_index(
        "ix_plan_allowances_org_period", "plan_allowances", ["org_id", "period_start"]
    )


def downgrade() -> None:
    op.drop_index("ix_plan_allowances_org_period", table_name="plan_allowances")
    op.drop_table("plan_allowances")
