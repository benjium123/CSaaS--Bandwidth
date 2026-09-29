"""P25 enterprise identity: sessions and login history.

Neither table is TenantScoped: a session belongs to a USER (org_id records which workspace
was active when it was issued, and may be NULL for the org-picker phase), and a login event
can precede any org. Routes that list them scope explicitly by user_id / org_id.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import GUID, PortableJSON

LOGIN_OUTCOMES: tuple[str, ...] = (
    "ok",
    "bad_password",
    "bad_2fa",
    "locked",
    "sso",
    "blocked_ip",
    "revoked",
)


class Session(Base, TimestampMixin):
    """One row per issued access token. The token carries this row's id as `sid`; the auth
    dependency rejects a revoked sid (cached 60 s in Redis to avoid a DB hit per request)."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    ip: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # --- P41 login risk + step-up -----------------------------------------------------
    #: services/login_risk.py flags raised when this session was issued ([] = clean).
    risk_flags: Mapped[list | None] = mapped_column(PortableJSON(), nullable=True)
    country: Mapped[str | None] = mapped_column(sa.String(2), nullable=True)
    #: Last time this session proved a second factor (login counts). auth/deps.py
    #: require_step_up("recent_2fa") reads it.
    second_factor_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # --- P42 cookie sessions -----------------------------------------------------------
    #: SHA-256 of the session secret held in the HttpOnly cookie (NULL = bearer-only row).
    token_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    #: password | password_totp | passkey | recovery_code | identity_recovery | sso
    auth_method: Mapped[str | None] = mapped_column(sa.String(24), nullable=True)
    # --- Ringlite apps P1: device sessions --------------------------------------------
    #: web | android | desktop | ios. NULL (rows issued before 0093) = web.
    device_kind: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    device_name: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    device_os: Mapped[str | None] = mapped_column(sa.String(60), nullable=True)
    app_version: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)
    #: SHA-256 of the current single-use refresh secret (device rows only).
    refresh_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    refresh_generation: Mapped[int | None] = mapped_column(sa.Integer(), nullable=True)
    #: The hash rotated out by the last refresh, and when: a replay of it inside a short grace
    #: window is a benign race (two windows, a lost response); after it, token theft.
    prev_refresh_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    refresh_rotated_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    @property
    def is_device(self) -> bool:
        return self.device_kind is not None and self.device_kind != "web"


class DeviceLinkCode(Base, TimestampMixin):
    """Ringlite apps P1: one-time QR code a signed-in web user shows to link a device."""

    __tablename__ = "device_link_codes"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    code_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True
    )
    #: The web session that created the code (its second_factor_at carries over).
    created_by_sid: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)


class LoginEvent(Base, TimestampMixin):
    __tablename__ = "login_events"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True
    )
    email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    ip: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    outcome: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    detail: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
