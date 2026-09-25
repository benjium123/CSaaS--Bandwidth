"""P44e: 911 service addresses (migration 0068).

Every number that can place calls must carry a validated emergency address at its carrier,
so a 911 call routes to the right dispatch centre and shows the right location. The
address lives here; ``carrier_refs`` holds each carrier's own address id
({"telnyx": "...", "signalwire": "..."}); the per-number state is on OrgNumber
(``emergency_address_id``, ``e911_status``).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

#: address status: pending (not yet sent), valid (carrier accepted), invalid (carrier refused)
ADDRESS_STATUSES = ("pending", "valid", "invalid")
#: per-number E911 status
E911_STATUSES = ("none", "pending", "active", "failed")


class EmergencyAddress(Base, TenantScoped, TimestampMixin):
    __tablename__ = "emergency_addresses"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="")
    caller_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    line1: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    line2: Mapped[str | None] = mapped_column(sa.String(128))
    city: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    state: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    postal_code: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    country: Mapped[str] = mapped_column(sa.String(2), nullable=False, default="US")
    carrier_refs: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(sa.String(255))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
