"""Billing v2: platform price list, message bundles, payments, refusals, org billing state.

Prices seeded (micros per unit): SMS $0.015/segment and MMS $0.035/message (in and out),
calls $0.012/minute (in and out), fax $0.10/page (in and out), number $15/month; bundles
$13 per 1,000 SMS, $3 per 100 MMS and $10 per 1,000 call minutes. Editable later from the
admin console.
"""

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0064_billing_v2"
down_revision = "0063_tendlc_registrations"
branch_labels = None
depends_on = None

SEED = {
    "sms_out": (15_000, "SMS segment sent (no bundle)"),
    "sms_in": (15_000, "SMS segment received (no bundle)"),
    "mms_out": (35_000, "MMS sent (no bundle)"),
    "mms_in": (35_000, "MMS received (no bundle)"),
    "voice_min_out": (12_000, "Outbound call minute, from answer, whole minutes"),
    "voice_min_in": (12_000, "Inbound call minute, from arrival, whole minutes, 1 min min"),
    "fax_page_out": (100_000, "Fax page sent"),
    "fax_page_in": (100_000, "Fax page received"),
    "number_mrc": (15_000_000, "Phone number per month"),
    "sms_bundle": (13_000_000, "1,000 SMS bundle; 5+ in one purchase = 20% off"),
    "mms_bundle": (3_000_000, "100 MMS bundle ($0.03 each); 5+ in one purchase = 10% off"),
    "voice_bundle": (10_000_000, "1,000 call minutes bundle; 5+ in one purchase = 10% off"),
}


def _ts():
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _org_fk():
    return sa.Column(
        "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False, index=True
    )


def upgrade():
    prices = op.create_table(
        "platform_prices",
        sa.Column("metric", sa.String(32), primary_key=True),
        sa.Column("price_micros", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.String(255)),
        sa.Column("updated_by", GUID()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(timezone.utc)
    op.bulk_insert(
        prices,
        [
            {"metric": k, "price_micros": v, "note": n, "updated_at": now}
            for k, (v, n) in SEED.items()
        ],
    )

    op.create_table(
        "bundle_ledger",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("entry_type", sa.String(16), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("delta_units", sa.BigInteger(), nullable=False),
        sa.Column("balance_after_units", sa.BigInteger(), nullable=False),
        sa.Column("reference", sa.String(128)),
        sa.Column("note", sa.String(255)),
        _ts(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("org_id", "kind", "entry_type", "reference", name="uq_bundle_ledger_ref"),
        sa.UniqueConstraint("org_id", "kind", "seq", name="uq_bundle_ledger_seq"),
    )

    op.create_table(
        "billing_payments",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("stripe_checkout_id", sa.String(255), unique=True),
        sa.Column("stripe_payment_intent_id", sa.String(255), unique=True),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("list_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("paid_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("discount_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("stripe_fee_micros", sa.BigInteger()),
        sa.Column("credited_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("units_credited", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("paid_at", sa.DateTime(timezone=True)),
        sa.Column("detail", PortableJSON()),
        _ts(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_billing_payments_org_created", "billing_payments", ["org_id", "created_at"]
    )

    op.create_table(
        "billing_refusals",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False, server_default="no_credit"),
        sa.Column("price_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("balance_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("detail", sa.String(255)),
        _ts(),
    )
    op.create_index(
        "ix_billing_refusals_org_created", "billing_refusals", ["org_id", "created_at"]
    )

    with op.batch_alter_table("orgs") as b:
        b.add_column(sa.Column("billing_state", sa.String(16), nullable=False, server_default="ok"))
        b.add_column(sa.Column("billing_state_changed_at", sa.DateTime(timezone=True)))
        b.add_column(
            sa.Column("avg_daily_spend_micros", sa.BigInteger(), nullable=False, server_default="0")
        )
        b.add_column(
            sa.Column(
                "warn_threshold_micros", sa.BigInteger(), nullable=False, server_default="5000000"
            )
        )
        b.add_column(sa.Column("low_balance_alert_key", sa.String(128)))
        b.add_column(
            sa.Column("auto_recharge_failures", sa.Integer(), nullable=False, server_default="0")
        )
        b.add_column(sa.Column("telnyx_billing_group_id", sa.String(64)))

    # Every past top-up becomes a payment row, so the console's revenue starts complete.
    op.execute(
        """
        INSERT INTO billing_payments
            (id, org_id, kind, state, stripe_payment_intent_id, quantity, list_micros,
             paid_micros, discount_micros, credited_micros, units_credited, paid_at,
             created_at, updated_at)
        SELECT id, org_id, 'topup', 'paid', reference, 1, amount_micros, amount_micros, 0,
               amount_micros, 0, created_at, created_at, created_at
        FROM credit_ledger
        WHERE entry_type = 'topup' AND reference IS NOT NULL
        """
    )


def downgrade():
    with op.batch_alter_table("orgs") as b:
        for col in (
            "telnyx_billing_group_id",
            "auto_recharge_failures",
            "low_balance_alert_key",
            "warn_threshold_micros",
            "avg_daily_spend_micros",
            "billing_state_changed_at",
            "billing_state",
        ):
            b.drop_column(col)
    op.drop_index("ix_billing_refusals_org_created", table_name="billing_refusals")
    op.drop_table("billing_refusals")
    op.drop_index("ix_billing_payments_org_created", table_name="billing_payments")
    op.drop_table("billing_payments")
    op.drop_table("bundle_ledger")
    op.drop_table("platform_prices")
