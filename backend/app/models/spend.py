"""P19: provider rate cards and derived daily spend.

Spend is an ESTIMATE computed from our own message/call/number records × a rate card,
never a bill. ``provider_rates`` holds org overrides only; code-level defaults apply when
no row exists. ``provider_spend_daily`` is derived and recomputed idempotently per day
(like ``usage_records``): a rollup never increments, it replaces the day's rows.
"""

from __future__ import annotations

import uuid
from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID

SPEND_METRICS: tuple[str, ...] = (
    "sms_out",
    "sms_in",
    "mms_out",
    "mms_in",
    "voice_min_out",
    "voice_min_in",
    "number_mrc",
    "number_setup",
)

#: Estimated public list prices (USD micros per unit) used when an org has no override.
#: Clearly an estimate; operators edit per-org rates in the Providers page.
DEFAULT_RATES_MICROS: dict[str, dict[str, int]] = {
    "bandwidth": {"sms_out": 4_000, "sms_in": 4_000, "mms_out": 15_000, "mms_in": 15_000,
                  "voice_min_out": 10_000, "voice_min_in": 5_500, "number_mrc": 350_000,
                  "number_setup": 0},
    "telnyx": {"sms_out": 4_000, "sms_in": 4_000, "mms_out": 15_000, "mms_in": 15_000,
               "voice_min_out": 7_000, "voice_min_in": 3_500, "number_mrc": 1_000_000,
               "number_setup": 1_000_000},
    "twilio": {"sms_out": 7_900, "sms_in": 7_900, "mms_out": 20_000, "mms_in": 20_000,
               "voice_min_out": 14_000, "voice_min_in": 8_500, "number_mrc": 1_150_000,
               "number_setup": 0},
    "plivo": {"sms_out": 5_000, "sms_in": 5_000, "mms_out": 15_000, "mms_in": 15_000,
              "voice_min_out": 10_000, "voice_min_in": 5_500, "number_mrc": 500_000,
              "number_setup": 0},
    "signalwire": {"sms_out": 4_000, "sms_in": 4_000, "mms_out": 12_000, "mms_in": 12_000,
                   "voice_min_out": 8_500, "voice_min_in": 5_000, "number_mrc": 1_000_000,
                   "number_setup": 0},
}


class ProviderRate(Base, TenantScoped, TimestampMixin):
    __tablename__ = "provider_rates"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "provider", "metric", name="uq_provider_rates_org_provider_metric"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    metric: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    #: Cost per unit in millionths of a dollar (1_000_000 = $1.00).
    unit_cost_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="USD")


class ProviderSpendDaily(Base, TenantScoped, TimestampMixin):
    __tablename__ = "provider_spend_daily"
    __table_args__ = (
        sa.UniqueConstraint(
            "org_id", "period_date", "provider", "metric", "number_id",
            name="uq_provider_spend_daily_key",
        ),
        sa.Index("ix_provider_spend_daily_org_date", "org_id", "period_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    period_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    provider: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    metric: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    quantity: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    cost_micros: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    #: Set for number-level rows (number_mrc / number_setup); NULL for traffic rows.
    number_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("org_numbers.id", ondelete="CASCADE"), nullable=True
    )
