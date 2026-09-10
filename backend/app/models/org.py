from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import GUID, PortableJSON


class Org(Base, TimestampMixin):
    """A tenant. Deliberately NOT TenantScoped — it is the tenant."""

    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    slug: Mapped[str] = mapped_column(sa.String(63), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    # P22: who may see a contact record. 'everyone' (default; pre-P22 behaviour),
    # 'department' (own + my departments' contacts), 'owner' (own + teams I lead).
    # contacts:read_all bypasses all three. Enforced by services/contact_visibility.py.
    contact_visibility: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="everyone", server_default="everyone"
    )

    # P23: 'platform' = CSaaS keys, billed per use (P24); 'byok' = the org's own AI keys
    # (ai_provider_accounts) - all three kinds must be active before an assistant goes live.
    ai_key_mode: Mapped[str] = mapped_column(
        sa.String(8), nullable=False, default="platform", server_default="platform"
    )

    # P24 billing knobs. ai_markup_bps NULL = platform default (billing.DEFAULT_AI_MARKUP_BPS);
    # ai_platform_fee_per_minute_micros applies to BYOK voice; credit_auto_recharge =
    # {threshold_micros, amount_micros, payment_method_id} or NULL.
    ai_markup_bps: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    ai_platform_fee_per_minute_micros: Mapped[int | None] = mapped_column(
        sa.BigInteger, nullable=True
    )
    credit_auto_recharge: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)

    # P25 security policy. require_2fa: members without 2FA get a setup interstitial after
    # require_2fa_grace_until; ip_allowlist: list of CIDR strings or NULL; sso:
    # {issuer, client_id, client_secret_encrypted, domain, enforce} or NULL.
    require_2fa: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    require_2fa_grace_until: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    ip_allowlist: Mapped[list | None] = mapped_column(PortableJSON(), nullable=True)
    sso: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)

    # P29: play a consent line before connecting (inbound via the flow engine, outbound via
    # the dialer). Text NULL = the platform default sentence.
    recording_announcement: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    recording_announcement_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    # P29: org-level calling settings that have no column of their own:
    # {"channel_layout": "mixed"|"dual", "dispositions": ["Interested", ...]}.
    # NULL = platform defaults (services/calling_settings.py).
    calling_settings: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)

    # P32 plans + invoicing (money: Fable-owned). plan_code NULL = no plan (prepaid credits only).
    plan_code: Mapped[str | None] = mapped_column(
        sa.String(32), sa.ForeignKey("plans.code", ondelete="SET NULL"), nullable=True
    )
    plan_started_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    billing_email: Mapped[str | None] = mapped_column(sa.String(320), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    # P33 agencies: a client workspace under an agency. Tenant scoping is unchanged (every
    # row still belongs to the CHILD org); parent access is an explicit cross-org grant
    # checked in one place (services/agency.py).
    parent_org_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:
        return f"<Org {self.slug}>"
