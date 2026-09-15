"""P32 money models (Fable-owned): plans and invoices. Integer micros only."""

from __future__ import annotations

import uuid
from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

INVOICE_STATUSES: tuple[str, ...] = ("draft", "open", "paid", "void")
PLAN_ALLOWANCE_KEYS: tuple[str, ...] = ("sms_segments", "voice_minutes", "numbers", "seats")


class Plan(Base, TimestampMixin):
    """Platform-wide catalogue (not tenant-scoped). Allowances are netted before overage."""

    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(sa.String(32), primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(63), nullable=False)
    monthly_price_micros: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
    included: Mapped[dict] = mapped_column(
        PortableJSON(), nullable=False, default=dict, server_default="{}"
    )
    overage_rates: Mapped[dict] = mapped_column(
        PortableJSON(), nullable=False, default=dict, server_default="{}"
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )


class PlanAllowance(Base, TenantScoped, TimestampMixin):
    """What one org's package includes this billing period, and how much of it is spent.

    Usage takes from here FIRST; whatever does not fit is overage, priced from the plan's
    ``overage_rates`` and charged against prepaid credits by ``services/telephony_billing``.
    The unique constraint is load-bearing: the take is a single bounded UPDATE against this
    row, so two concurrent sends can never both take the last included text.

    ``included_units`` is snapshotted when the period opens rather than read live from
    ``plans.included`` - editing a plan must never retroactively change what an org was
    already given this month.
    """

    __tablename__ = "plan_allowances"
    __table_args__ = (
        sa.UniqueConstraint(
            "org_id", "period_start", "metric", name="uq_plan_allowances_org_period_metric"
        ),
        sa.Index("ix_plan_allowances_org_period", "org_id", "period_start"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    #: The org's plan_started_at anniversary, not the calendar month.
    period_start: Mapped[date] = mapped_column(sa.Date, nullable=False)
    metric: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    included_units: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
    used_units: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )


class Invoice(Base, TenantScoped, TimestampMixin):
    __tablename__ = "invoices"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "period_start", name="uq_invoices_org_period"),
        sa.Index("ix_invoices_org_status", "org_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    #: P33: the parent org that pays for this child; NULL = the org pays for itself.
    billed_to_org_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
    )
    period_start: Mapped[date] = mapped_column(sa.Date, nullable=False)
    period_end: Mapped[date] = mapped_column(sa.Date, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(8), nullable=False, default="draft", server_default="draft"
    )
    subtotal_micros: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
    tax_micros: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
    total_micros: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
    currency: Mapped[str] = mapped_column(
        sa.String(3), nullable=False, default="USD", server_default="USD"
    )
    stripe_invoice_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    pdf_key: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)


class InvoiceLine(Base, TenantScoped, TimestampMixin):
    __tablename__ = "invoice_lines"
    __table_args__ = (sa.Index("ix_invoice_lines_invoice", "invoice_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False
    )
    metric: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    description: Mapped[str] = mapped_column(
        sa.String(255), nullable=False, default="", server_default=""
    )
    quantity: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
    included_quantity: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
    unit_price_micros: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
    amount_micros: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
