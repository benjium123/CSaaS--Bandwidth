"""Billing v2 (Fable/Opus-owned money models): editable platform prices, message bundles,
payments as the customer paid them, and refused-for-credit attempts.

- ``platform_prices`` is the operator's price list (micros per unit). No row = the constant
  in services/telephony_billing.PLATFORM_PRICE_MICROS.
- ``bundle_ledger`` is APPEND-ONLY, same discipline as credit_ledger: one row per change,
  ``balance_after_units`` per (org, kind), idempotent on (org, kind, entry_type, reference).
  Bundles are prepaid message units (1 unit = 1 SMS segment or 1 MMS), spent before the
  $ balance.
- ``billing_payments`` records every card payment with list price, discount and Stripe fee,
  so the admin console can show revenue and profit without asking Stripe.
- ``billing_refusals`` counts what the credit gate stopped (it leaves no other row).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

BUNDLE_KINDS: tuple[str, ...] = ("sms", "mms")
BUNDLE_ENTRY_TYPES: tuple[str, ...] = ("purchase", "usage", "adjustment", "refund")
PAYMENT_KINDS: tuple[str, ...] = ("topup", "sms_bundle", "mms_bundle", "auto_recharge")
PAYMENT_STATES: tuple[str, ...] = ("pending", "paid", "failed", "refunded")
REFUSAL_KINDS: tuple[str, ...] = ("sms", "mms", "call", "inbound_call", "number", "fax")


class PlatformPrice(Base):
    __tablename__ = "platform_prices"

    metric: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    price_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    note: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class BundleLedgerEntry(Base, TenantScoped, TimestampMixin):
    __tablename__ = "bundle_ledger"
    __table_args__ = (
        sa.UniqueConstraint(
            "org_id", "kind", "entry_type", "reference", name="uq_bundle_ledger_ref"
        ),
        sa.UniqueConstraint("org_id", "kind", "seq", name="uq_bundle_ledger_seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    entry_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    seq: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    #: Signed units. purchase/refund-of-usage > 0; usage < 0.
    delta_units: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    balance_after_units: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    reference: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)


class BillingPayment(Base, TenantScoped, TimestampMixin):
    __tablename__ = "billing_payments"
    __table_args__ = (sa.Index("ix_billing_payments_org_created", "org_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="pending")
    stripe_checkout_id: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    quantity: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    #: What the purchase lists at before any discount.
    list_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    #: What the customer actually paid.
    paid_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    discount_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    #: Stripe's processing fee, filled by the fee tick from the balance transaction.
    stripe_fee_micros: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    #: $ credited to the balance (top-ups) - 0 for bundles.
    credited_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    #: Message units credited (bundles) - 0 for top-ups.
    units_credited: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    paid_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    detail: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)


class BillingRefusal(Base, TenantScoped):
    __tablename__ = "billing_refusals"
    __table_args__ = (sa.Index("ix_billing_refusals_org_created", "org_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    reason: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="no_credit")
    price_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    balance_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    detail: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class TelnyxCostDaily(Base):
    """Telnyx's own billed cost per org per day (nightly detail-records reconciliation).
    org_id NULL = traffic on the shared account that matched none of our numbers (the CRM's,
    or a released number) - kept for the account total, never shown per org."""

    __tablename__ = "telnyx_cost_daily"
    __table_args__ = (
        sa.Index("ix_telnyx_cost_daily_date", "period_date"),
        sa.Index("ix_telnyx_cost_daily_org_date", "org_id", "period_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
    )
    period_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    record_type: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="")
    quantity: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    cost_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
