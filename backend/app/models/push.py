"""P31: Web Push subscriptions (VAPID) per user and device."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

#: Five event toggles plus one digest switch (Fable decision, P31 plan).
NOTIFICATION_PREF_KEYS: tuple[str, ...] = (
    "mention",
    "assignment",
    "new_inbound",
    "missed_call",
    "sla_breach",
    "digest",
)
DEFAULT_NOTIFICATION_PREFS: dict[str, bool] = dict.fromkeys(NOTIFICATION_PREF_KEYS, True)


class PushSubscription(Base, TenantScoped, TimestampMixin):
    __tablename__ = "push_subscriptions"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "endpoint", name="uq_push_subscriptions_user_endpoint"),
        sa.Index("ix_push_subscriptions_org_user", "org_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    #: {"p256dh": ..., "auth": ...} exactly as the browser hands them over.
    keys: Mapped[dict] = mapped_column(
        PortableJSON(), nullable=False, default=dict, server_default="{}"
    )
    user_agent: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
