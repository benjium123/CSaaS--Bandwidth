"""P42 enterprise SSO: verified email domains and SCIM provisioning tokens.

A workspace may only enforce SSO for, and automatically link people from, an email domain it
has PROVEN it controls (DNS TXT record). Before this, any workspace could type any domain
into its SSO settings and have its identity provider log people from that domain into it.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID


class OrgDomain(Base, TenantScoped, TimestampMixin):
    __tablename__ = "org_domains"
    __table_args__ = (sa.UniqueConstraint("org_id", "domain", name="uq_org_domains_org_domain"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    domain: Mapped[str] = mapped_column(sa.String(253), nullable=False, index=True)
    #: Value the workspace publishes as TXT ``_csaas-verify.<domain>`` = ``csaas-verify=<token>``.
    verify_token: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ScimToken(Base, TenantScoped, TimestampMixin):
    """Bearer token an identity provider uses to create and remove this workspace's users.
    Stored like an API key: prefix for lookup, SHA-256 of the whole token."""

    __tablename__ = "scim_tokens"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(127), nullable=False)
    prefix: Mapped[str] = mapped_column(sa.String(16), nullable=False, unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
