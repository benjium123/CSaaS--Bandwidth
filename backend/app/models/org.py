from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import GUID


class Org(Base, TimestampMixin):
    """A tenant. Deliberately NOT TenantScoped — it is the tenant."""

    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    slug: Mapped[str] = mapped_column(sa.String(63), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f"<Org {self.slug}>"
