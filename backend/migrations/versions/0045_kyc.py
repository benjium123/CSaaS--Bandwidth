"""P41b/c business verification (KYC), ban list, Stripe event ledger.

- kyc_profiles        one application per org (status, business, use case, risk, limits)
- kyc_persons         owners / beneficial owners / admin / billing people and their ID checks
- kyc_documents       encrypted business documents (object-store key, never the bytes)
- kyc_checks          automatic and manual check results
- kyc_step_ups        selfie re-checks before risky actions
- fraud_identifiers   platform-wide ban list (hashed values)
- stripe_events       Stripe webhook replay ledger
- payment_methods.card_fingerprint

BACKFILL: every org that exists before this migration gets an ``approved`` profile with
decision_reason ``grandfathered`` - those workspaces were onboarded by hand before
verification existed, and switching KYC_ENFORCED on must not cut off their live traffic.
Re-verification is scheduled a year out like any approval.

Revision ID: 0045_kyc
Revises: 0044_account_security
Create Date: 2026-09-16
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0045_kyc"
down_revision = "0044_account_security"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _org_fk() -> sa.Column:
    return sa.Column(
        "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )


def _user_fk(name: str) -> sa.Column:
    return sa.Column(name, GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


def upgrade() -> None:
    op.create_table(
        "kyc_profiles",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("country", sa.String(2), nullable=True),
        sa.Column("legal_name", sa.String(255), nullable=True),
        sa.Column("dba_name", sa.String(255), nullable=True),
        sa.Column("entity_type", sa.String(24), nullable=True),
        sa.Column("registration_number", sa.String(64), nullable=True),
        sa.Column("tax_id", sa.String(64), nullable=True),
        sa.Column("incorporation_date", sa.Date(), nullable=True),
        sa.Column("registered_address", PortableJSON(), nullable=True),
        sa.Column("operating_address", PortableJSON(), nullable=True),
        sa.Column("website", sa.String(255), nullable=True),
        sa.Column("business_email", sa.String(320), nullable=True),
        sa.Column("business_phone", sa.String(32), nullable=True),
        sa.Column("use_case", PortableJSON(), nullable=True),
        sa.Column("use_case_pending", PortableJSON(), nullable=True),
        sa.Column("risk_tier", sa.String(16), nullable=True),
        sa.Column("risk_reasons", PortableJSON(), nullable=True),
        sa.Column(
            "submitted_from_flagged_login", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("video_call_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("video_call_done_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("video_call_by"),
        sa.Column("video_call_note", sa.Text(), nullable=True),
        sa.Column("deposit_required_cents", sa.Integer(), nullable=True),
        sa.Column("limits", PortableJSON(), nullable=True),
        sa.Column("agreement_version", sa.String(32), nullable=True),
        sa.Column("agreement_accepted_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("agreement_accepted_by"),
        sa.Column("agreement_ip", sa.String(64), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("submitted_by"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("decided_by"),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("info_request", sa.Text(), nullable=True),
        sa.Column("next_reverification_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("suspended_by"),
        sa.Column("suspension_reason", sa.Text(), nullable=True),
        sa.Column("status_before_suspension", sa.String(24), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("org_id", name="uq_kyc_profiles_org"),
    )
    op.create_index("ix_kyc_profiles_status", "kyc_profiles", ["status"])

    op.create_table(
        "kyc_persons",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("role", sa.String(24), nullable=False),
        _user_fk("user_id"),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("ownership_percent", sa.Integer(), nullable=True),
        sa.Column("stripe_verification_session_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="not_started"),
        sa.Column("verified_name", sa.String(255), nullable=True),
        sa.Column("document_country", sa.String(2), nullable=True),
        sa.Column("document_type", sa.String(32), nullable=True),
        sa.Column("identity_hash", sa.String(64), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(255), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "stripe_verification_session_id", name="uq_kyc_persons_stripe_session"
        ),
    )
    op.create_index("ix_kyc_persons_org_id", "kyc_persons", ["org_id"])
    op.create_index("ix_kyc_persons_user_id", "kyc_persons", ["user_id"])
    op.create_index("ix_kyc_persons_identity_hash", "kyc_persons", ["identity_hash"])

    op.create_table(
        "kyc_documents",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(255), nullable=False),
        _user_fk("uploaded_by"),
        *_timestamps(),
    )
    op.create_index("ix_kyc_documents_org_id", "kyc_documents", ["org_id"])

    op.create_table(
        "kyc_checks",
        sa.Column("id", GUID(), primary_key=True),
        _org_fk(),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False, server_default=""),
        sa.Column("detail", PortableJSON(), nullable=True),
        _user_fk("created_by"),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_kyc_checks_org_created", "kyc_checks", ["org_id", "created_at"])

    op.create_table(
        "kyc_step_ups",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("stripe_verification_session_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("identity_hash", sa.String(64), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(255), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "stripe_verification_session_id", name="uq_kyc_step_ups_stripe_session"
        ),
    )
    op.create_index("ix_kyc_step_ups_user_id", "kyc_step_ups", ["user_id"])

    op.create_table(
        "fraud_identifiers",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("value_hash", sa.String(64), nullable=False),
        sa.Column("display_hint", sa.String(64), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column(
            "source_org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="SET NULL"), nullable=True
        ),
        _user_fk("created_by"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint("kind", "value_hash", name="uq_fraud_identifiers_kind_value"),
    )
    op.create_index("ix_fraud_identifiers_value_hash", "fraud_identifiers", ["value_hash"])

    op.create_table(
        "stripe_events",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("type", sa.String(128), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.add_column("payment_methods", sa.Column("card_fingerprint", sa.String(64), nullable=True))

    # ---- backfill: grandfather every existing org -------------------------------------
    conn = op.get_bind()
    org_ids = [row[0] for row in conn.execute(sa.text("SELECT id FROM orgs")).fetchall()]
    if org_ids:
        now = datetime.now(timezone.utc)
        profiles = sa.table(
            "kyc_profiles",
            sa.column("id", GUID()),
            sa.column("org_id", GUID()),
            sa.column("status", sa.String()),
            sa.column("risk_tier", sa.String()),
            sa.column("decided_at", sa.DateTime(timezone=True)),
            sa.column("decision_reason", sa.Text()),
            sa.column("next_reverification_at", sa.DateTime(timezone=True)),
            sa.column("created_at", sa.DateTime(timezone=True)),
            sa.column("updated_at", sa.DateTime(timezone=True)),
        )
        op.bulk_insert(
            profiles,
            [
                {
                    "id": uuid.uuid4(),
                    "org_id": org_id if isinstance(org_id, uuid.UUID) else uuid.UUID(str(org_id)),
                    "status": "approved",
                    "risk_tier": "standard",
                    "decided_at": now,
                    "decision_reason": "grandfathered",
                    "next_reverification_at": now + timedelta(days=365),
                    "created_at": now,
                    "updated_at": now,
                }
                for org_id in org_ids
            ],
        )


def downgrade() -> None:
    op.drop_column("payment_methods", "card_fingerprint")
    op.drop_table("stripe_events")
    op.drop_index("ix_fraud_identifiers_value_hash", table_name="fraud_identifiers")
    op.drop_table("fraud_identifiers")
    op.drop_index("ix_kyc_step_ups_user_id", table_name="kyc_step_ups")
    op.drop_table("kyc_step_ups")
    op.drop_index("ix_kyc_checks_org_created", table_name="kyc_checks")
    op.drop_table("kyc_checks")
    op.drop_index("ix_kyc_documents_org_id", table_name="kyc_documents")
    op.drop_table("kyc_documents")
    op.drop_index("ix_kyc_persons_identity_hash", table_name="kyc_persons")
    op.drop_index("ix_kyc_persons_user_id", table_name="kyc_persons")
    op.drop_index("ix_kyc_persons_org_id", table_name="kyc_persons")
    op.drop_table("kyc_persons")
    op.drop_index("ix_kyc_profiles_status", table_name="kyc_profiles")
    op.drop_table("kyc_profiles")
