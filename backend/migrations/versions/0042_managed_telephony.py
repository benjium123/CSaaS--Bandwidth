"""P37 managed telephony (reseller): one Telnyx managed sub-account per org.

- telephony_accounts        one row per org. Every id the provisioning engine creates in
                            the org's Telnyx sub-account and in LiveKit, so a re-run skips
                            finished steps ("Repair setup"). Secrets (the sub-account API
                            key, the SIP password) are stored encrypted with
                            CREDENTIALS_MASTER_KEY, never returned by any API.
- org_numbers.provisioning  per-number assignment state: messaging profile, voice
                            connection, LiveKit trunk registration, 10DLC campaign.

Additive. Chains off 0041_prepaid_telephony.

Revision ID: 0042_managed_telephony
Revises: 0041_prepaid_telephony
Create Date: 2026-09-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0042_managed_telephony"
down_revision = "0041_prepaid_telephony"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telephony_accounts",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False, server_default="telnyx"),
        # provisioning | active | suspended | failed
        sa.Column("status", sa.String(16), nullable=False, server_default="provisioning"),
        sa.Column("managed_account_id", sa.String(64), nullable=True),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("messaging_profile_id", sa.String(64), nullable=True),
        sa.Column("outbound_voice_profile_id", sa.String(64), nullable=True),
        sa.Column("sip_connection_id", sa.String(64), nullable=True),
        sa.Column("sip_username", sa.String(64), nullable=True),
        sa.Column("sip_password_encrypted", sa.Text(), nullable=True),
        sa.Column("livekit_outbound_trunk_id", sa.String(64), nullable=True),
        sa.Column("livekit_dispatch_rule_id", sa.String(64), nullable=True),
        sa.Column("tendlc_brand_id", sa.String(64), nullable=True),
        sa.Column("tendlc_campaign_id", sa.String(64), nullable=True),
        sa.Column("last_step", sa.String(32), nullable=True),
        sa.Column("last_error", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("org_id", name="uq_telephony_accounts_org"),
    )
    op.add_column(
        "org_numbers",
        sa.Column("provisioning", PortableJSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("org_numbers", "provisioning")
    op.drop_table("telephony_accounts")
