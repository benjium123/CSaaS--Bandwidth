"""P41: per-workspace daily messaging rollup.

One row per (org, day, carrier). Counters only - every rate (delivery, spam-block,
opt-out) is derived when read, so a receipt that arrives a day late corrects the counts and
the rate follows. Same shape discipline as ProviderSpendDaily: a unique key so two rollups
of the same day collide instead of doubling.
"""

from __future__ import annotations

import uuid
from datetime import date

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID

#: The vocabulary `messages.failure_class` is allowed to hold. Anything a carrier reports
#: that we cannot place lands in "unknown" and is COUNTED there - never guessed into a
#: nicer bucket.
FAILURE_CLASSES: tuple[str, ...] = (
    "spam_blocked",
    "carrier_rejected",
    "invalid_destination",
    "opted_out",
    "unknown",
)


class OrgMessagingDaily(Base, TenantScoped, TimestampMixin):
    __tablename__ = "org_messaging_daily"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "period_date", "carrier", name="uq_org_messaging_daily_key"),
        sa.Index("ix_org_messaging_daily_org_date", "org_id", "period_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    period_date: Mapped[date] = mapped_column(sa.Date, nullable=False)
    carrier: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    #: Outbound messages created that day (any status).
    sent: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    delivered: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    #: All carrier-terminal failures (status failed or rejected), whatever the class.
    failed: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    failed_spam_blocked: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    failed_carrier_rejected: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0
    )
    failed_invalid_destination: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0
    )
    failed_opted_out: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    inbound: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    #: ConsentEvent rows that day by event: opt_out / help_request / opt_in.
    opt_outs: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    help_requests: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    opt_ins: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
