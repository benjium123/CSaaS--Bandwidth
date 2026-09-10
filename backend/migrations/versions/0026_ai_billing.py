"""P24: AI metering, pricing, prepaid credits (Fable-only money schema).

- ai_usage_events        append-only per-call/turn metering rows with cost (what we pay) and
                         price (what the customer pays); idempotent on idempotency_key
- provider_rates         + price_micros (sell rate; NULL = derive from cost x markup)
                         + scope ('traffic' | 'ai'); provider domain widens to AI providers
- orgs                   + ai_markup_bps (per-org override of the platform default),
                         + ai_platform_fee_per_minute_micros (BYOK voice fee),
                         + credit_auto_recharge JSON
- credit_ledger          append-only signed entries with balance_after; balance = last row
- payment_methods        Stripe customer/payment-method references (no card data)

All additive. Revision chains after 0025_ai_assistant_product.

Revision ID: 0026_ai_billing
Revises: 0025_ai_assistant_product
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0026_ai_billing"
down_revision = "0025_ai_assistant_product"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_usage_events",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("call_id", GUID(), sa.ForeignKey("calls.id", ondelete="SET NULL"), nullable=True),
        sa.Column(
            "thread_id",
            GUID(),
            sa.ForeignKey("message_threads.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "profile_id",
            GUID(),
            sa.ForeignKey("agent_profiles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),  # llm | stt | tts | voice
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False),
        sa.Column("cost_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("price_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(16), nullable=False),  # worker | simulate | sms_agent
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_ai_usage_events_idempotency"),
    )
    op.create_index("ix_ai_usage_events_org_occurred", "ai_usage_events", ["org_id", "occurred_at"])
    op.create_index("ix_ai_usage_events_org_call", "ai_usage_events", ["org_id", "call_id"])

    op.add_column("provider_rates", sa.Column("price_micros", sa.BigInteger(), nullable=True))
    op.add_column(
        "provider_rates",
        sa.Column("scope", sa.String(8), nullable=False, server_default="traffic"),
    )

    op.add_column("orgs", sa.Column("ai_markup_bps", sa.Integer(), nullable=True))
    op.add_column(
        "orgs", sa.Column("ai_platform_fee_per_minute_micros", sa.BigInteger(), nullable=True)
    )
    op.add_column("orgs", sa.Column("credit_auto_recharge", PortableJSON(), nullable=True))

    op.create_table(
        "credit_ledger",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        # topup | usage | adjustment | refund | reserve | release
        sa.Column("entry_type", sa.String(16), nullable=False),
        # Strict per-org order (timestamps can tie); assigned under the per-org lock.
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("amount_micros", sa.BigInteger(), nullable=False),
        sa.Column("balance_after_micros", sa.BigInteger(), nullable=False),
        sa.Column("reference", sa.String(128), nullable=True),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column(
            "created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # A reference (Stripe intent id, usage event id, reserve token) is charged once.
        sa.UniqueConstraint(
            "org_id", "entry_type", "reference", name="uq_credit_ledger_org_type_ref"
        ),
        sa.UniqueConstraint("org_id", "seq", name="uq_credit_ledger_org_seq"),
    )
    op.create_index("ix_credit_ledger_org_created", "credit_ledger", ["org_id", "created_at"])

    op.create_table(
        "payment_methods",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stripe_customer_id", sa.String(64), nullable=False),
        sa.Column("stripe_payment_method_id", sa.String(64), nullable=False),
        sa.Column("brand", sa.String(16), nullable=False, server_default=""),
        sa.Column("last4", sa.String(4), nullable=False, server_default=""),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "org_id", "stripe_payment_method_id", name="uq_payment_methods_org_pm"
        ),
    )
    op.create_index("ix_payment_methods_org_id", "payment_methods", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_methods_org_id", table_name="payment_methods")
    op.drop_table("payment_methods")
    op.drop_index("ix_credit_ledger_org_created", table_name="credit_ledger")
    op.drop_table("credit_ledger")
    op.drop_column("orgs", "credit_auto_recharge")
    op.drop_column("orgs", "ai_platform_fee_per_minute_micros")
    op.drop_column("orgs", "ai_markup_bps")
    op.drop_column("provider_rates", "scope")
    op.drop_column("provider_rates", "price_micros")
    op.drop_index("ix_ai_usage_events_org_call", table_name="ai_usage_events")
    op.drop_index("ix_ai_usage_events_org_occurred", table_name="ai_usage_events")
    op.drop_table("ai_usage_events")
