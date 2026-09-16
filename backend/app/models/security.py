"""P41 account security: passkeys, login risk, security alerts, platform operators.

None of these tables is TenantScoped. A passkey, a device and an operator belong to a USER
(one person, many orgs); a security alert may precede any org context. Every query filters
explicitly by user_id - the same rule as models/identity.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import GUID, PortableJSON

#: Why a login was flagged. Kept as a closed set so the operator queue and the owner alert
#: can explain every flag in plain words (services/login_risk.py::FLAG_LABELS).
LOGIN_RISK_FLAGS: tuple[str, ...] = (
    "country_not_allowed",
    "tor",
    "datacenter",
    "new_device",
    "country_changed",
)

WEBAUTHN_PURPOSES: tuple[str, ...] = ("register", "login", "step_up")

SECURITY_ALERT_KINDS: tuple[str, ...] = ("flagged_login", "limit_request", "sanctions_hit")
SECURITY_ALERT_STATUSES: tuple[str, ...] = ("open", "reviewed")

#: reviewer = read the KYC queue and decide applications; admin = also suspend orgs,
#: manage the ban list and add/remove operators.
OPERATOR_ROLES: tuple[str, ...] = ("reviewer", "admin")


class UserPasskey(Base, TimestampMixin):
    """One WebAuthn credential. Only the public key is stored - the private key never leaves
    the user's device, which is the whole point of a passkey."""

    __tablename__ = "user_passkeys"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: base64url credential id, unique across the platform.
    credential_id: Mapped[str] = mapped_column(sa.String(512), nullable=False, unique=True)
    public_key: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    sign_count: Mapped[int] = mapped_column(sa.BigInteger, nullable=False, default=0)
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="Passkey")
    transports: Mapped[list | None] = mapped_column(PortableJSON(), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class WebauthnChallenge(Base, TimestampMixin):
    """Server-side, single-use challenge. Consumed on the first verify attempt, right or
    wrong, so a captured assertion can never be replayed against it."""

    __tablename__ = "webauthn_challenges"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    challenge: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class LoginDevice(Base, TimestampMixin):
    """A device a user has signed in from. The browser generates a random id (X-Device-Id)
    and we keep only its SHA-256, so this table cannot be used to impersonate a device."""

    __tablename__ = "login_devices"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "device_hash", name="uq_login_devices_user_device"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_ip: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    last_country: Mapped[str | None] = mapped_column(sa.String(2), nullable=True)


class SecurityAlert(Base, TimestampMixin):
    """An item in the operator review queue that is not a KYC application."""

    __tablename__ = "security_alerts"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="open", server_default="open", index=True
    )
    #: Never secrets: flags, ip, country, asn org, user agent.
    detail: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    review_note: Mapped[str | None] = mapped_column(sa.String(500), nullable=True)


class PlatformOperator(Base, TimestampMixin):
    """A real, named platform operator. Replaces the shared ops token for anything that
    touches identity documents or decides who may use the platform: every action then has
    a person attached in the audit log."""

    __tablename__ = "platform_operators"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    role: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="reviewer")
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
