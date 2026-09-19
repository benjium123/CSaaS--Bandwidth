"""P41a account security: passkeys, login risk, security alerts, platform operators.

- user_passkeys         WebAuthn credentials (public key only)
- webauthn_challenges   single-use server-side challenges
- login_devices         SHA-256 of the browser's random device id, per user
- security_alerts       operator review queue items that are not KYC applications
- platform_operators    named operators replacing the shared ops token for KYC/PII routes
- users.has_passkey     denormalised flag read by the per-request 2FA gate
- sessions.risk_flags / country / second_factor_at   login risk + step-up proof

Additive. Existing users have no passkey (has_passkey false); existing sessions have no
second_factor_at, so an operator must sign in again before the console answers.

Revision ID: 0044_account_security
Revises: 0043_plan_allowances
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0044_account_security"
down_revision = "0043_plan_allowances"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("has_passkey", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("sessions", sa.Column("risk_flags", PortableJSON(), nullable=True))
    op.add_column("sessions", sa.Column("country", sa.String(2), nullable=True))
    op.add_column(
        "sessions", sa.Column("second_factor_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "user_passkeys",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("credential_id", sa.String(512), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("name", sa.String(64), nullable=False, server_default="Passkey"),
        sa.Column("transports", PortableJSON(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("credential_id", name="uq_user_passkeys_credential_id"),
    )
    op.create_index("ix_user_passkeys_user_id", "user_passkeys", ["user_id"])

    op.create_table(
        "webauthn_challenges",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_webauthn_challenges_user_id", "webauthn_challenges", ["user_id"])

    op.create_table(
        "login_devices",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("device_hash", sa.String(64), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_ip", sa.String(64), nullable=True),
        sa.Column("last_country", sa.String(2), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "device_hash", name="uq_login_devices_user_device"),
    )
    op.create_index("ix_login_devices_user_id", "login_devices", ["user_id"])

    op.create_table(
        "security_alerts",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column(
            "org_id", GUID(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("detail", PortableJSON(), nullable=True),
        sa.Column(
            "reviewed_by",
            GUID(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(500), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_security_alerts_user_id", "security_alerts", ["user_id"])
    op.create_index("ix_security_alerts_org_id", "security_alerts", ["org_id"])
    op.create_index("ix_security_alerts_status", "security_alerts", ["status"])

    op.create_table(
        "platform_operators",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column(
            "user_id", GUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("role", sa.String(16), nullable=False, server_default="reviewer"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint("user_id", name="uq_platform_operators_user_id"),
    )


def downgrade() -> None:
    op.drop_table("platform_operators")
    op.drop_index("ix_security_alerts_status", table_name="security_alerts")
    op.drop_index("ix_security_alerts_org_id", table_name="security_alerts")
    op.drop_index("ix_security_alerts_user_id", table_name="security_alerts")
    op.drop_table("security_alerts")
    op.drop_index("ix_login_devices_user_id", table_name="login_devices")
    op.drop_table("login_devices")
    op.drop_index("ix_webauthn_challenges_user_id", table_name="webauthn_challenges")
    op.drop_table("webauthn_challenges")
    op.drop_index("ix_user_passkeys_user_id", table_name="user_passkeys")
    op.drop_table("user_passkeys")
    op.drop_column("sessions", "second_factor_at")
    op.drop_column("sessions", "country")
    op.drop_column("sessions", "risk_flags")
    op.drop_column("users", "has_passkey")
