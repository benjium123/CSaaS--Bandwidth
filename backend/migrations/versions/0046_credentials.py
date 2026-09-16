"""P42 credential lifecycle: password reset, recovery codes, lockout, account audit.

- password_reset_tokens   single-use, SHA-256 of the emailed token
- recovery_codes          SHA-256 of one-time codes
- account_lockouts        progressive lock state per user
- account_audit_log       per-person security history (no org needed)
- users.step_up_blocked_until, users.password_changed_at

Additive; no backfill.

Revision ID: 0046_credentials
Revises: 0045_kyc
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0046_credentials"
down_revision = "0045_kyc"
branch_labels = None
depends_on = None


def _ts() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _user_fk(name: str = "user_id", *, nullable: bool = False, cascade: bool = True) -> sa.Column:
    return sa.Column(
        name,
        GUID(),
        sa.ForeignKey("users.id", ondelete="CASCADE" if cascade else "SET NULL"),
        nullable=nullable,
    )


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("step_up_blocked_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "users", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", GUID(), primary_key=True),
        _user_fk(),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip", sa.String(64), nullable=True),
        *_ts(),
        sa.UniqueConstraint("token_hash", name="uq_password_reset_tokens_hash"),
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])

    op.create_table(
        "recovery_codes",
        sa.Column("id", GUID(), primary_key=True),
        _user_fk(),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
    )
    op.create_index("ix_recovery_codes_user_id", "recovery_codes", ["user_id"])

    op.create_table(
        "account_lockouts",
        sa.Column("id", GUID(), primary_key=True),
        _user_fk(),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_locked_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
        sa.UniqueConstraint("user_id", name="uq_account_lockouts_user_id"),
    )

    op.create_table(
        "account_audit_log",
        sa.Column("id", GUID(), primary_key=True),
        _user_fk(),
        _user_fk("actor_user_id", nullable=True, cascade=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(255), nullable=True),
        sa.Column("detail", PortableJSON(), nullable=True),
    )
    op.create_index("ix_account_audit_user_at", "account_audit_log", ["user_id", "at"])
    # Lockout counts failed login events per user in a window - served by the existing
    # ix_login_events_user_at index from 0027.


def downgrade() -> None:
    op.drop_index("ix_account_audit_user_at", table_name="account_audit_log")
    op.drop_table("account_audit_log")
    op.drop_table("account_lockouts")
    op.drop_index("ix_recovery_codes_user_id", table_name="recovery_codes")
    op.drop_table("recovery_codes")
    op.drop_index("ix_password_reset_tokens_user_id", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_column("users", "password_changed_at")
    op.drop_column("users", "step_up_blocked_until")
