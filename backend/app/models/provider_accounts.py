"""P17: per-org provider (carrier) accounts with encrypted credentials.

One row per (org, provider) in P17. ``credentials_encrypted`` is a Fernet token of a
JSON object whose keys mirror the ``Settings`` attribute names for that provider (so
adapters are constructed with no renaming). Plaintext credentials never touch a log, an
audit row, or a GET response — services/credentials.py is the only decrypt site.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID

PROVIDER_NAMES: tuple[str, ...] = ("bandwidth", "telnyx", "twilio", "plivo", "signalwire")
PROVIDER_ACCOUNT_STATUSES: tuple[str, ...] = ("unverified", "active", "failed", "disabled")

#: Field catalogue per provider. ``secret`` fields are write-only (never echoed).
PROVIDER_CREDENTIAL_FIELDS: dict[str, dict[str, bool]] = {
    # name -> {field: is_secret}
    "bandwidth": {
        "account_id": False,
        "api_username": False,
        "api_password": True,
        "messaging_application_id": False,
        "voice_application_id": False,
        "webhook_username": False,
        "webhook_password": True,
    },
    "telnyx": {
        "api_key": True,
        "public_key": True,
        "messaging_profile_id": False,
        "voice_connection_id": False,
    },
    "twilio": {"account_sid": False, "auth_token": True, "messaging_service_sid": False},
    "plivo": {"auth_id": False, "auth_token": True, "powerpack_uuid": False},
    "signalwire": {"project_id": False, "api_token": True, "space_url": False},
}


class ProviderAccount(Base, TenantScoped, TimestampMixin):
    __tablename__ = "provider_accounts"
    __table_args__ = (
        sa.UniqueConstraint("org_id", "provider", name="uq_provider_accounts_org_provider"),
        sa.Index("ix_provider_accounts_provider_status", "provider", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    label: Mapped[str] = mapped_column(sa.String(127), nullable=False, default="")
    credentials_encrypted: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="unverified", server_default="unverified"
    )
    last_probe_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_probe_detail: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:
        return f"<ProviderAccount {self.provider} {self.status}>"
