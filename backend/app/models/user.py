from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import GUID


class User(Base, TimestampMixin):
    """A login. GLOBAL, not org-scoped: one person, many orgs.

    There is deliberately no ``is_superuser`` flag — the superuser/user binary is exactly
    what we refused to inherit from the FastAPI template. Platform administration is a
    P13/P14 concern with its own model.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(sa.String(255), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f"<User {self.email}>"
