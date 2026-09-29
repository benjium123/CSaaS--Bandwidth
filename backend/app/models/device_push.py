"""Ringlite apps P2: native push (FCM) tokens per device session.

Not TenantScoped: a token belongs to a USER's device session (like ``sessions``), not to
a workspace. ``session_id`` is unique, so a device session holds at most one FCM token
and a re-registration replaces the previous value in place.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import GUID


class DevicePushToken(Base, TimestampMixin):
    __tablename__ = "device_push_tokens"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        sa.ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    #: android | desktop | ios
    platform: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    #: The FCM registration token. Opaque to us; never logged.
    token: Mapped[str] = mapped_column(sa.String(4096), nullable=False)
    app_version: Mapped[str | None] = mapped_column(sa.String(40), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
