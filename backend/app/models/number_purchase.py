"""Durable cart and payment/provisioning state for recurring phone-number purchases."""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON


class NumberPurchase(Base, TenantScoped, TimestampMixin):
    __tablename__ = "number_purchases"
    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    numbers: Mapped[list] = mapped_column(PortableJSON(), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(32), default="checkout", nullable=False)
    checkout_id: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    checkout_url: Mapped[str | None] = mapped_column(sa.Text())
    #: Not unique: on a workspace plan every purchase shares the workspace's one subscription.
    subscription_id: Mapped[str | None] = mapped_column(sa.String(255), index=True)
    #: The workspace plan this cart was bought on (services/plan_billing.py); None for the
    #: retired per-number price.
    plan_code: Mapped[str | None] = mapped_column(sa.String(32))
    #: "month" or "year": how the first purchase's plan is billed.
    billing_interval: Mapped[str] = mapped_column(
        sa.String(8), nullable=False, default="month", server_default="month"
    )
    subscription_status: Mapped[str | None] = mapped_column(sa.String(32))
    detail: Mapped[str | None] = mapped_column(sa.Text())
    #: Where these numbers' 911 calls are sent (E911 registered location).
    emergency_address_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("emergency_addresses.id", ondelete="SET NULL")
    )
    #: When the buyer acknowledged the limits of 911 over VoIP (47 CFR 9.11(a)(5)).
    e911_acknowledged_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
