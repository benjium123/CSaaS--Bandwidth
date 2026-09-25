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
    __table_args__ = (
        sa.CheckConstraint(
            "account_type IN ('business', 'individual')",
            name="ck_orgs_account_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    slug: Mapped[str] = mapped_column(sa.String(63), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    number_subscription_required: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    # Account classification: 'business' (a company workspace, the default and pre-existing
    # behaviour) or 'individual' (a single-person account created by register). Immutable at
    # the API level — nothing reclassifies an existing org. No ORM mutation hook on purpose:
    # the registration flow stamps account_type AFTER the repository creates the row.
    account_type: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="business", server_default="business"
    )
    # P59: open registration lets anyone create an individual workspace, so new orgs are
    # gated behind identity verification; migration 0059 grandfathers existing orgs to false.
    kyc_required: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
    #: P42: stricter-than-platform session timeouts for this workspace (NULL = platform).
    session_idle_minutes: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    session_max_hours: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    #: P42: accept this workspace's SSO sessions as phishing-resistant for privileged roles
    #: (the identity provider enforces MFA).
    trust_idp_mfa: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
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
    # Prepaid telephony hard gate (migration 0041). When true, outbound SMS/MMS, outbound
    # calls and number orders draw from the prepaid credit balance and are refused when
    # it cannot cover them; inbound traffic and number rental are charged. ON by default
    # since migration 0055 (pay-as-you-go credits are the money gate); platform ops can
    # switch a specific org off, and repositories/orgs.create_org_with_owner is what
    # stamps telephony_prepaid_since for a new org.
    telephony_prepaid: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
    #: When the gate was last switched on - calls that started before it are never billed.
    telephony_prepaid_since: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

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

    # Billing v2 (migration 0064). billing_state: ok | low | exhausted, recomputed by the
    # credits tick; warn_threshold = max($5, average daily spend over 7 days).
    billing_state: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="ok", server_default="ok"
    )
    billing_state_changed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    avg_daily_spend_micros: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=0, server_default="0"
    )
    warn_threshold_micros: Mapped[int] = mapped_column(
        sa.BigInteger, nullable=False, default=5_000_000, server_default="5000000"
    )
    #: Dedupe key of the last low-balance alert sent (one alert per level per top-up).
    low_balance_alert_key: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    #: Consecutive declined auto-recharges; 3 switches auto-recharge off.
    auto_recharge_failures: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )
    #: Telnyx billing group for this org's csaas-tagged numbers (cost grouping only).
    telnyx_billing_group_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    def __repr__(self) -> str:
        return f"<Org {self.slug}>"
