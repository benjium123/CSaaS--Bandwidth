"""Per-workspace feature switches (P2). A missing row means the catalog default applies
(services/entitlements.py CATALOG), so only explicit operator choices are stored."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID


class OrgFeature(Base, TenantScoped, TimestampMixin):
    __tablename__ = "org_features"
    __table_args__ = (sa.UniqueConstraint("org_id", "feature_key", name="uq_org_features_key"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    feature_key: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    #: Per-workspace price for this feature's metric; NULL = the platform price.
    price_override_micros: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
