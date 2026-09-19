"""P42 enterprise SSO: verified domains and SCIM tokens.

- org_domains   DNS-verified email domains per workspace
- scim_tokens   hashed provisioning tokens

SAML settings live in the existing ``orgs.sso`` JSON (``protocol: "saml"``), next to OIDC.

With SSO_REQUIRE_VERIFIED_DOMAIN on, existing SSO setups stop signing people in until the
workspace verifies its domain (RUNBOOK: "Enterprise SSO"). Password sign-in is unaffected,
and SSO enforcement is ignored for unverified domains, so nobody is locked out.

Revision ID: 0049_enterprise_sso
Revises: 0048_api_key_networks
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID

revision = "0049_enterprise_sso"
down_revision = "0048_api_key_networks"
branch_labels = None
depends_on = None


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "org_domains",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("domain", sa.String(253), nullable=False),
        sa.Column("verify_token", sa.String(64), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        *_ts(),
        sa.UniqueConstraint("org_id", "domain", name="uq_org_domains_org_domain"),
    )
    op.create_index("ix_org_domains_org_id", "org_domains", ["org_id"])
    op.create_index("ix_org_domains_domain", "org_domains", ["domain"])

    op.create_table(
        "scim_tokens",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(127), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("prefix", name="uq_scim_tokens_prefix"),
    )
    op.create_index("ix_scim_tokens_org_id", "scim_tokens", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_scim_tokens_org_id", table_name="scim_tokens")
    op.drop_table("scim_tokens")
    op.drop_index("ix_org_domains_domain", table_name="org_domains")
    op.drop_index("ix_org_domains_org_id", table_name="org_domains")
    op.drop_table("org_domains")
