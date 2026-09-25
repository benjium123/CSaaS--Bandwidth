"""P44f: number porting (migration 0069).

A PortRequest is either a customer asking to bring numbers IN (``direction="in"``) or a
port-out we detected at the carrier for one of our numbers (``direction="out"``).

Port-in statuses:
  awaiting_review  customer submitted; an operator must check it (port-in hijack defence)
  submitted        sent to the carrier (Telnyx) - or, for SignalWire, filed by hand
  in_process       carrier working on it
  exception        carrier needs something fixed (see last_error / events)
  foc_confirmed    carrier committed a date (``foc_date``)
  ported           numbers are ours; imported into org_numbers
  rejected         operator refused it
  cancelled        customer or carrier cancelled
Port-out statuses: pending, ported, rejected, cancelled.

``secret_enc`` holds the losing carrier's account PIN, encrypted with the credentials key;
it is never returned by the API.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

PORT_IN_STATUSES = (
    "awaiting_review",
    "submitted",
    "in_process",
    "exception",
    "foc_confirmed",
    "ported",
    "rejected",
    "cancelled",
)
OPEN_PORT_IN_STATUSES = ("awaiting_review", "submitted", "in_process", "exception", "foc_confirmed")


class PortRequest(Base, TenantScoped, TimestampMixin):
    __tablename__ = "port_requests"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    direction: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    carrier: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    numbers: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    status: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    carrier_ref: Mapped[str | None] = mapped_column(sa.String(64))
    foc_date: Mapped[str | None] = mapped_column(sa.String(32))
    details: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    secret_enc: Mapped[str | None] = mapped_column(sa.Text)
    loa_media_key: Mapped[str | None] = mapped_column(sa.String(255))
    invoice_media_key: Mapped[str | None] = mapped_column(sa.String(255))
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(sa.String(255))
    events: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
