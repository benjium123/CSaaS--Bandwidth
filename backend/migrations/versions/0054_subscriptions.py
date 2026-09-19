"""Subscriptions: a plan's Stripe price id, and the local mirror of a Stripe subscription.

- plans.stripe_price_id   the recurring Stripe Price (``price_...``) this plan is sold as.
                          NULL for every existing and seeded row, deliberately: pricing is
                          not decided, and a plan with no price id CANNOT be checked out.
                          The operator fills these in once real Prices exist in Stripe.
- subscriptions           org_id, plan_code, the Stripe subscription/customer ids, status,
                          period end and cancel_at_period_end, written only from verified
                          Stripe webhooks.

Uniqueness on stripe_subscription_id is expressed as a unique INDEX rather than a table
constraint: webhook handling upserts on that column, and SQLite cannot add a UNIQUE
constraint with ALTER TABLE (same reasoning as 0053). The model declares it as a
UniqueConstraint, which create_all renders inline - the same guarantee under a different
name, so an autogenerate run may report the naming as drift.

No existing row is touched: the new column is nullable and the new table starts empty.
Telephony behaviour is unchanged by this migration - REQUIRE_SUBSCRIPTION_FOR_TELEPHONY
defaults to false.

Revision ID: 0054_subscriptions
Revises: 0053_didit_identity
Create Date: 2026-09-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0054_subscriptions"
down_revision = "0053_didit_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plans", sa.Column("stripe_price_id", sa.String(64), nullable=True))

    op.create_table(
        "subscriptions",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "plan_code",
            sa.String(32),
            sa.ForeignKey("plans.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stripe_subscription_id", sa.String(64), nullable=False),
        sa.Column("stripe_customer_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="incomplete"),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_subscriptions_org_id", "subscriptions", ["org_id"])
    op.create_index(
        "uq_subscriptions_stripe_subscription_id",
        "subscriptions",
        ["stripe_subscription_id"],
        unique=True,
    )
    op.create_index("ix_subscriptions_org_status", "subscriptions", ["org_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_subscriptions_org_status", table_name="subscriptions")
    op.drop_index("uq_subscriptions_stripe_subscription_id", table_name="subscriptions")
    op.drop_index("ix_subscriptions_org_id", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_column("plans", "stripe_price_id")
