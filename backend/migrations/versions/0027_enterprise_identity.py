"""P25: enterprise identity - sessions, login history, org security policy.

- sessions        one row per issued access token (sid claim); revoked_at makes the token
                  dead within the deps cache window (60 s)
- login_events    every login attempt and outcome, per user (org_id NULL = pre-org login)
- orgs            + require_2fa, + require_2fa_grace_until, + ip_allowlist JSON, + sso JSON
                  ({issuer, client_id, client_secret_encrypted, domain, enforce})

Additive. Revision chains after 0026_ai_billing.

Revision ID: 0027_enterprise_identity
Revises: 0026_ai_billing
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0027_enterprise_identity"
down_revision = "0026_ai_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked_by", GUID(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_org_id", "sessions", ["org_id"])

    op.create_table(
        "login_events",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        # ok | bad_password | bad_2fa | locked | sso | blocked_ip | revoked
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("detail", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_login_events_user_at", "login_events", ["user_id", "at"])
    op.create_index("ix_login_events_org_at", "login_events", ["org_id", "at"])

    op.add_column(
        "orgs", sa.Column("require_2fa", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        "orgs", sa.Column("require_2fa_grace_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("orgs", sa.Column("ip_allowlist", PortableJSON(), nullable=True))
    op.add_column("orgs", sa.Column("sso", PortableJSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("orgs", "sso")
    op.drop_column("orgs", "ip_allowlist")
    op.drop_column("orgs", "require_2fa_grace_until")
    op.drop_column("orgs", "require_2fa")
    op.drop_index("ix_login_events_org_at", table_name="login_events")
    op.drop_index("ix_login_events_user_at", table_name="login_events")
    op.drop_table("login_events")
    op.drop_index("ix_sessions_org_id", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
