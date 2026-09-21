from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import GUID, PortableJSON


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
    email_verification_required: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    email_verification_hash: Mapped[str | None] = mapped_column(sa.String(64), unique=True)
    email_verification_expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True)
    )
    email_verification_sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    # --- P2 2FA/TOTP ------------------------------------------------------------
    # Fernet-encrypted with CREDENTIAL_ENCRYPTION_KEY - the first real consumer of that
    # key. There is deliberately NO plaintext fallback branch: branching secret storage
    # is bug bait, so enrollment 503s when the key is absent.
    totp_secret: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    # Blocks replay of a code that was just accepted.
    totp_last_used_step: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    #: P41: denormalised "has at least one passkey" so the per-request 2FA gate in
    #: auth/deps.py never needs a query. Maintained only by services/passkeys.py.
    has_passkey: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )

    #: P42: after an identity recovery, sensitive (step-up) actions stay blocked until this
    #: time - a stolen account recovered by an attacker cannot immediately be emptied.
    step_up_blocked_until: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    #: P42: first time this account was seen with owner/admin/billing/operator power; the
    #: passkey grace period counts from here (services/passkey_policy.py).
    passkey_required_since: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    @property
    def has_second_factor(self) -> bool:
        return bool(self.totp_enabled or self.has_passkey)

    # P31: {mention, assignment, new_inbound, missed_call, sla_breach, digest: bool}; NULL =
    # every toggle on (models/push.py::DEFAULT_NOTIFICATION_PREFS).
    notification_prefs: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)

    def __repr__(self) -> str:
        return f"<User {self.email}>"
