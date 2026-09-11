"""P37 managed telephony (Fable-owned schema): one Telnyx managed sub-account per org.

Secrets (`api_key_encrypted`, `sip_password_encrypted`) are encrypted with
CREDENTIALS_MASTER_KEY via services/credentials and are never returned by any API.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID

TELEPHONY_ACCOUNT_STATUSES: tuple[str, ...] = ("provisioning", "active", "suspended", "failed")


class TelephonyAccount(Base, TenantScoped, TimestampMixin):
    __tablename__ = "telephony_accounts"
    __table_args__ = (sa.UniqueConstraint("org_id", name="uq_telephony_accounts_org"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="telnyx", server_default="telnyx"
    )
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="provisioning", server_default="provisioning"
    )
    managed_account_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    api_key_encrypted: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    messaging_profile_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    outbound_voice_profile_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    sip_connection_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    sip_username: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    sip_password_encrypted: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    livekit_outbound_trunk_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    livekit_dispatch_rule_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    tendlc_brand_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    tendlc_campaign_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    #: The last provisioning step that completed; a re-run resumes after it.
    last_step: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    #: Plain-English reason the last run stopped, shown to the customer.
    last_error: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
