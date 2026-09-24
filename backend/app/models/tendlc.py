"""Self-serve 10DLC: one customer's paid registration, from checkout to an active campaign."""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON


class TenDlcRegistration(Base, TenantScoped, TimestampMixin):
    """Drives the existing brand/campaign filing services on the customer's behalf.

    ``stage`` is where the automation is, not the carrier's verdict - the Brand and
    Campaign rows keep their own statuses and approval evidence exactly as before:
      checkout -> paid -> brand_filed -> [otp_pending] -> brand_approved -> campaign_filed
      -> active, or brand_rejected / campaign_rejected / needs_attention / expired.
    """

    __tablename__ = "tendlc_registrations"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    brand_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("brands.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("campaigns.id", ondelete="RESTRICT"), nullable=False
    )
    #: "standard" ($10/mo) or "sole_proprietor" ($2/mo) - which pass-through fee applies.
    fee_tier: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    stage: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="checkout")
    #: What filing needs that Brand/Campaign do not store: names, relationship, the sole
    #: proprietor's mobile, and the campaign attestations the customer made at checkout.
    filing: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    checkout_id: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    checkout_url: Mapped[str | None] = mapped_column(sa.Text())
    subscription_id: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    paid_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    otp_sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    evidence_checked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(sa.Text())
