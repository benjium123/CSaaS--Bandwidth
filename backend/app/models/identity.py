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
from app.db.types import GUID

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
