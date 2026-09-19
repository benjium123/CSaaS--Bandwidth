"""P41b business verification (KYC/KYB) and the platform ban list.

Tenant-scoped (one org's application): kyc_profiles, kyc_persons, kyc_documents, kyc_checks.
Platform-wide on purpose (NOT TenantScoped):
  - kyc_step_ups      belong to a person, not a workspace
  - fraud_identifiers the ban list must match ACROSS orgs - that is its entire job
  - stripe_events     webhook replay ledger

No identity document image is ever stored here. Stripe Identity keeps the ID and selfie;
we keep the outcome, the verified name, the document country/type, and a SHA-256 of
name + date of birth so the same person can be recognised again without storing either.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, TimestampMixin
from app.db.types import GUID, PortableJSON

KYC_STATUSES: tuple[str, ...] = (
    "draft",
    "submitted",
    "in_review",
    "needs_info",
    "approved",
    "rejected",
    "suspended",
    "reverification_due",
)

#: Statuses in which calling, texting and number orders are allowed.
KYC_TELEPHONY_STATUSES: frozenset[str] = frozenset({"approved", "reverification_due"})

#: Statuses in which the customer may still edit the application.
KYC_EDITABLE_STATUSES: frozenset[str] = frozenset({"draft", "needs_info"})

KYC_ENTITY_TYPES: tuple[str, ...] = (
    "corporation",
    "llc",
    "partnership",
    "sole_proprietor",
    "nonprofit",
    "government",
    "other",
)

KYC_PERSON_ROLES: tuple[str, ...] = ("owner", "beneficial_owner", "admin", "billing")
KYC_PERSON_STATUSES: tuple[str, ...] = (
    "not_started",
    "pending",
    "processing",
    "verified",
    "requires_input",
    "canceled",
)

KYC_DOCUMENT_KINDS: tuple[str, ...] = (
    "registration_certificate",
    "tax_id_letter",
    "articles",
    "proof_of_address",
    "other",
)

KYC_CHECK_KINDS: tuple[str, ...] = (
    "registry",
    "sanctions",
    "ban_list",
    "website",
    "email_domain",
    "name_match",
    "ai_summary",
    # P43: AI review of every uploaded document, and the AI decision pack for the operator.
    "documents",
    "ai_decision",
)

#: P43: outcome of the AI read of one uploaded document.
KYC_DOCUMENT_REVIEW_RESULTS: tuple[str, ...] = ("pass", "warn", "fail", "error")
KYC_CHECK_RESULTS: tuple[str, ...] = ("pass", "warn", "fail", "error", "pending")

KYC_RISK_TIERS: tuple[str, ...] = ("standard", "high")

#: Actions that need a fresh selfie (auth/deps.py require_step_up("recent_selfie", ...)).
STEP_UP_ACTIONS: tuple[str, ...] = (
    "payment_method_change",
    "limit_increase",
    "bulk_number_order",
    "api_key_create",
    "admin_grant",
    "ownership_transfer",
    "use_case_change",
    # P42: proving who you are when every sign-in factor is lost.
    "account_recovery",
)
STEP_UP_STATUSES: tuple[str, ...] = ("pending", "processing", "verified", "failed", "canceled")

FRAUD_IDENTIFIER_KINDS: tuple[str, ...] = (
    "email",
    "email_domain",
    "phone",
    "device",
    "card_fingerprint",
    "address",
    "registration_number",
    "tax_id",
    "person",
    "website_domain",
    "ip",
)


class KycProfile(Base, TenantScoped, TimestampMixin):
    __tablename__ = "kyc_profiles"
    __table_args__ = (sa.UniqueConstraint("org_id", name="uq_kyc_profiles_org"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(
        sa.String(24), nullable=False, default="draft", server_default="draft", index=True
    )

    # --- the business ------------------------------------------------------------------
    country: Mapped[str | None] = mapped_column(sa.String(2), nullable=True)
    legal_name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    dba_name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    entity_type: Mapped[str | None] = mapped_column(sa.String(24), nullable=True)
    #: EIN / state file number (US), Corporation or BN (CA), Companies House number (UK).
    registration_number: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    incorporation_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    #: {line1, line2, city, region, postal_code, country}
    registered_address: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    operating_address: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    website: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    business_email: Mapped[str | None] = mapped_column(sa.String(320), nullable=True)
    business_phone: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)

    #: The declared use case - the baseline later call-content and traffic monitoring
    #: compare real behaviour against. {description, vertical, who_you_contact,
    #: list_source, monthly_calls, monthly_texts, destination_countries, sample_script}
    use_case: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    #: A change requested after approval waits here for an operator; the approved
    #: use_case stays in force until then.
    use_case_pending: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)

    # --- risk --------------------------------------------------------------------------
    risk_tier: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    risk_reasons: Mapped[list | None] = mapped_column(PortableJSON(), nullable=True)
    #: The submitter's session was flagged by login risk when they submitted.
    submitted_from_flagged_login: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    video_call_required: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    video_call_done_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    video_call_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    video_call_note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    # --- deposit and starting limits (values decided later; NULL = not enforced) --------
    deposit_required_cents: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    #: {daily_calls, daily_texts, max_numbers}
    limits: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)

    # --- agreement -----------------------------------------------------------------------
    agreement_version: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    agreement_accepted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    agreement_accepted_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    agreement_ip: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    # --- lifecycle ------------------------------------------------------------------------
    submitted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decision_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: What the reviewer asked the customer for (status needs_info).
    info_request: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    next_reverification_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    suspended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    suspended_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    suspension_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: Status to return to on unsuspend.
    status_before_suspension: Mapped[str | None] = mapped_column(sa.String(24), nullable=True)


class KycPerson(Base, TenantScoped, TimestampMixin):
    __tablename__ = "kyc_persons"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    role: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    full_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(sa.String(320), nullable=True)
    ownership_percent: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    stripe_verification_session_id: Mapped[str | None] = mapped_column(
        sa.String(64), nullable=True, unique=True
    )
    #: P44: the provider-neutral pair. ``identity_provider`` is "stripe" or "didit" and
    #: ``provider_session_id`` is that provider's own session id. The Stripe column above
    #: stays authoritative for Stripe rows (existing rows have only it), so neither column
    #: replaces it - they sit alongside it.
    identity_provider: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    provider_session_id: Mapped[str | None] = mapped_column(
        sa.String(128), nullable=True, unique=True
    )
    status: Mapped[str] = mapped_column(
        sa.String(24), nullable=False, default="not_started", server_default="not_started"
    )
    verified_name: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    document_country: Mapped[str | None] = mapped_column(sa.String(2), nullable=True)
    document_type: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    #: sha256(normalised full name | yyyy-mm-dd). Recognises the same person across orgs
    #: and step-ups without storing the date of birth.
    identity_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)
    verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    #: P43: where the person lives now (an ID card's address is often out of date). Proven by
    #: a proof_of_address document linked to this person.
    residential_address: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)


class KycDocument(Base, TenantScoped, TimestampMixin):
    __tablename__ = "kyc_documents"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    filename: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: Object-store key; the stored bytes are Fernet ciphertext.
    storage_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    #: P43: the owner a proof_of_address belongs to.
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("kyc_persons.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: P43: AI document review - result, what was read, and why.
    review_result: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    review: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class KycCheck(Base, TenantScoped, TimestampMixin):
    __tablename__ = "kyc_checks"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    result: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    summary: Mapped[str] = mapped_column(sa.String(500), nullable=False, default="")
    detail: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    #: Set when an operator recorded the result by hand (e.g. a manual registry lookup).
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    tokens_in: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)


class KycStepUp(Base, TimestampMixin):
    __tablename__ = "kyc_step_ups"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    stripe_verification_session_id: Mapped[str | None] = mapped_column(
        sa.String(64), nullable=True, unique=True
    )
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default="pending", server_default="pending"
    )
    identity_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)


class FraudIdentifier(Base, TimestampMixin):
    """The ban list. Values are stored as SHA-256 of the normalised value, so the list can
    be matched without keeping a readable copy of a banned person's details."""

    __tablename__ = "fraud_identifiers"
    __table_args__ = (
        sa.UniqueConstraint("kind", "value_hash", name="uq_fraud_identifiers_kind_value"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    value_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: Non-sensitive hint for operators, e.g. "***@example.com" or "…4242".
    display_hint: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    reason: Mapped[str] = mapped_column(sa.String(500), nullable=False)
    source_org_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)


class IdentityWebhookEvent(Base):
    """Every identity-provider webhook event id we have processed - durable replay
    protection for providers other than Stripe.

    Stripe keeps its own ledger (``stripe_events``) and that table is not reused here:
    Stripe event ids and Didit event ids come from different issuers, so one primary key
    space shared between them could collide, and the two ledgers have different retention
    and different blast radii.
    """

    __tablename__ = "identity_webhook_events"

    #: "<provider>:<event_id>" so two providers can never collide on an id.
    id: Mapped[str] = mapped_column(sa.String(320), primary_key=True)
    provider: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    event_type: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    received_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class StripeEvent(Base):
    """Every Stripe webhook event id we have processed - durable replay protection."""

    __tablename__ = "stripe_events"

    id: Mapped[str] = mapped_column(sa.String(255), primary_key=True)
    type: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    received_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
