"""P42 credential lifecycle: password reset tokens, recovery codes, lockouts, account audit.

None of these is TenantScoped - they belong to a person (one user, many workspaces).
Every secret is stored as a SHA-256 hash: reset tokens and recovery codes are 128+ bits of
randomness, so a fast hash is the right tool (argon2 would only slow down legitimate use).
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import GUID, PortableJSON

ACCOUNT_AUDIT_ACTIONS: tuple[str, ...] = (
    "password.changed",
    "password.reset_requested",
    "password.reset",
    "totp.enabled",
    "totp.disabled",
    "email_2fa.enabled",
    "email_2fa.disabled",
    "passkey.added",
    "passkey.removed",
    "recovery_codes.generated",
    "recovery_code.used",
    "account.recovered_identity",
    "factors.reset_by_admin",
    "factors.reset_by_operator",
    "sessions.revoked",
    "account.locked",
    "account.unlocked",
    "account.deactivated",
    "account.reactivated",
    "account.created_by_admin",
)


class PasswordResetToken(Base, TimestampMixin):
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    requested_ip: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)


class RecoveryCode(Base, TimestampMixin):
    __tablename__ = "recovery_codes"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class AccountLockout(Base, TimestampMixin):
    """One row per user while locked (or recently locked - ``level`` drives escalation)."""

    __tablename__ = "account_lockouts"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    locked_until: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    level: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_locked_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class AccountAuditEntry(Base):
    """Security history of one person's account, independent of any workspace."""

    __tablename__ = "account_audit_log"
    __table_args__ = (sa.Index("ix_account_audit_user_at", "user_id", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    #: Who did it: the user themself, an org admin, or a platform operator. Never secrets.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(sa.String(48), nullable=False)
    at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    ip: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    detail: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
