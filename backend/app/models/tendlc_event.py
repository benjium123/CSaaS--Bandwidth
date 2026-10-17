"""10DLC phase 1: every carrier event about a workspace's brand, campaign or numbers."""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped
from app.db.types import GUID, PortableJSON


class TenDlcEvent(Base, TenantScoped):
    """One Telnyx 10DLC event (webhook) or one local carrier step we want on the timeline.

    ``verified`` is False when the webhook's ed25519 signature was missing or wrong: the row
    is kept (the reason text is useful), but nothing ever changes state on its word alone."""

    __tablename__ = "tendlc_events"
    __table_args__ = (
        sa.UniqueConstraint("telnyx_event_id", name="uq_tendlc_events_telnyx_event_id"),
        sa.Index("ix_tendlc_events_registration", "registration_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    registration_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("tendlc_registrations.id", ondelete="CASCADE"), nullable=True
    )
    #: "brand" | "campaign" | "number"
    subject: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    #: Telnyx brandId / campaignId, or the E.164 number.
    telnyx_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: Telnyx's event id (data.id); NULL for locally recorded steps. Dedupes retries.
    telnyx_event_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    payload_type: Mapped[str | None] = mapped_column(sa.String(48), nullable=True)
    status: Mapped[str | None] = mapped_column(sa.String(48), nullable=True)
    reasons: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    verified: Mapped[bool] = mapped_column(sa.Boolean(), nullable=False, default=False)
    raw: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    received_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
