"""Per-org carrier routing policy.

One row per org, created lazily with safe defaults. The defaults matter more than the
table: an org that has never opened the routing screen must behave exactly as it did before
phase-3b, which means *no cross-carrier failover* unless somebody deliberately turned it on.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON


class RoutingPolicy(Base, TenantScoped, TimestampMixin):
    """How this org picks a carrier when nothing more specific has been said."""

    __tablename__ = "routing_policies"
    __table_args__ = (sa.UniqueConstraint("org_id", name="uq_routing_policy_org"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    #: Ordered carrier names, most preferred first. Empty = use the registry's order.
    preference: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)

    #: Try another number on the SAME carrier when a send is rejected retryably.
    #: Safe by default: same brand, same registration, same attestation.
    allow_intra_carrier_failover: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True
    )

    #: Try a DIFFERENT carrier - which necessarily means a different number, so the
    #: recipient sees a different sender. Off by default, and never applied mid-thread
    #: (phase-3b DR-1).
    allow_cross_carrier_failover: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False
    )

    #: Pin every send to one carrier regardless of preference/health. The "at will"
    #: override at org scope; an operator can still override per request.
    pinned_carrier: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
