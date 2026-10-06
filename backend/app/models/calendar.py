"""Calendar connections (2026-10-07): an external calendar (Google first) a workspace
member connected so the AI books around their real busy times and writes each booking
into it.

Ringlite's own calendar (``appointments``) stays the source of truth. A connection only
ADDS busy times and RECEIVES copies of bookings; losing one never loses an appointment.

``credentials`` is Fernet ciphertext (``services.credentials.encrypt``) of
``{"refresh_token", "access_token", "expires_at"}`` and is never returned by any API.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID

CALENDAR_PROVIDERS = ("google",)
#: active = usable; error = the provider refused our token (reconnect needed).
CALENDAR_STATUSES = ("active", "error")


class CalendarConnection(Base, TenantScoped, TimestampMixin):
    __tablename__ = "calendar_connections"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    #: The member who connected it. SET NULL: the calendar keeps working for the
    #: workspace's assistants after that member is removed, until someone disconnects it.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    account_email: Mapped[str] = mapped_column(sa.String(320), nullable=False)
    #: The provider's calendar id; "primary" = the account's main calendar.
    calendar_id: Mapped[str] = mapped_column(
        sa.String(255), nullable=False, default="primary", server_default="primary"
    )
    credentials: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="active", server_default="active"
    )
    last_error: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    __table_args__ = (
        sa.UniqueConstraint(
            "org_id", "provider", "account_email", name="uq_calendar_connections_account"
        ),
    )
