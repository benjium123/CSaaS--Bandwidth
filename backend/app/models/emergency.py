"""E911: the registered location a 911 call from a number is sent to."""

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID


class EmergencyAddress(Base, TenantScoped, TimestampMixin):
    """A carrier-validated street address a workspace registers its numbers at.

    Interconnected VoIP must send 911 calls with the caller's registered location, so every
    number carries one (``OrgNumber.provisioning["e911"]`` records which, and the carrier's
    provisioning status). One address usually serves many numbers - an office.
    """

    __tablename__ = "emergency_addresses"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    #: Telnyx /v2/addresses id; the carrier routes 911 by this.
    telnyx_address_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: The business or person at the location, as the 911 dispatcher will see it.
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    street_address: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    #: Suite, floor, apartment - dispatch needs it in a multi-tenant building.
    extended_address: Mapped[str | None] = mapped_column(sa.String(255))
    locality: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    administrative_area: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    postal_code: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    country_code: Mapped[str] = mapped_column(sa.String(2), nullable=False, default="US")
    #: P44e (migration 0070): SignalWire's id for this address, created the first time a
    #: SignalWire number is registered at it.
    signalwire_address_id: Mapped[str | None] = mapped_column(sa.String(64))
