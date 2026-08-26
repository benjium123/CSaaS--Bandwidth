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

    # --- P2 2FA/TOTP ------------------------------------------------------------
    # Fernet-encrypted with CREDENTIAL_ENCRYPTION_KEY - the first real consumer of that
    # key. There is deliberately NO plaintext fallback branch: branching secret storage
    # is bug bait, so enrollment 503s when the key is absent.
    totp_secret: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    # Blocks replay of a code that was just accepted.
    totp_last_used_step: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)

    def __repr__(self) -> str:
        return f"<User {self.email}>"
