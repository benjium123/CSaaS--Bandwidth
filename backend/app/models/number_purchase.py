"""Durable cart and payment/provisioning state for recurring phone-number purchases."""

import uuid

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
    subscription_id: Mapped[str | None] = mapped_column(sa.String(255), unique=True)
    subscription_status: Mapped[str | None] = mapped_column(sa.String(32))
    detail: Mapped[str | None] = mapped_column(sa.Text())
