"""P32 plans, traffic billing, invoices (Fable-only money schema).

- plans                 catalogue: monthly price, included allowances, overage rates
- orgs                  + plan_code, plan_started_at, billing_email, tax_id
- invoices              monthly statements (draft | open | paid | void), Stripe invoice id, PDF key
- invoice_lines         one line per metric on an invoice
- billed_to_org_id      on invoices: a parent org (P33 agencies) may pay for a child

All money in integer micros. Additive. Revision chains after 0033_notifications_push.

Revision ID: 0034_plans_invoices
Revises: 0033_notifications_push
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0034_plans_invoices"
down_revision = "0033_notifications_push"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("code", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("monthly_price_micros", sa.BigInteger(), nullable=False, server_default="0"),
        # {"sms_segments": int, "voice_minutes": int, "numbers": int, "seats": int}
        sa.Column("included", PortableJSON(), nullable=False, server_default="{}"),
        # {"sms_segments": micros_per_unit, ...}
        sa.Column("overage_rates", PortableJSON(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.add_column(
        "orgs",
        sa.Column(
            "plan_code",
            sa.String(32),
            sa.ForeignKey("plans.code", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("orgs", sa.Column("plan_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("orgs", sa.Column("billing_email", sa.String(320), nullable=True))
    op.add_column("orgs", sa.Column("tax_id", sa.String(64), nullable=True))

    op.create_table(
        "invoices",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "billed_to_org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(8), nullable=False, server_default="draft"),
        sa.Column("subtotal_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tax_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("stripe_invoice_id", sa.String(64), nullable=True),
        sa.Column("pdf_key", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", "period_start", name="uq_invoices_org_period"),
    )
    op.create_index("ix_invoices_org_status", "invoices", ["org_id", "status"])

    op.create_table(
        "invoice_lines",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "invoice_id", GUID(), sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("description", sa.String(255), nullable=False, server_default=""),
        sa.Column("quantity", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("included_quantity", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("unit_price_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("amount_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_invoice_lines_invoice", "invoice_lines", ["invoice_id"])


def downgrade() -> None:
    op.drop_index("ix_invoice_lines_invoice", table_name="invoice_lines")
    op.drop_table("invoice_lines")
    op.drop_index("ix_invoices_org_status", table_name="invoices")
    op.drop_table("invoices")
    op.drop_column("orgs", "tax_id")
    op.drop_column("orgs", "billing_email")
    op.drop_column("orgs", "plan_started_at")
    op.drop_column("orgs", "plan_code")
    op.drop_table("plans")
