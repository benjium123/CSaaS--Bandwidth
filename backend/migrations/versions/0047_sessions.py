"""P42 cookie sessions, workspace session policy, passkeys for privileged accounts.

- sessions.token_hash        SHA-256 of the HttpOnly cookie secret (NULL = bearer-only row)
- sessions.auth_method       how the session was established or last stepped up
- orgs.session_idle_minutes / session_max_hours   stricter per-workspace timeouts
- orgs.trust_idp_mfa         accept SSO sessions for passkey-required roles
- users.passkey_required_since   start of the passkey grace period

Additive. Existing sessions are bearer-only (token_hash NULL) and simply age out; people
sign in again and receive a cookie session.

Revision ID: 0047_sessions
Revises: 0046_credentials
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047_sessions"
down_revision = "0046_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("token_hash", sa.String(64), nullable=True))
    op.add_column("sessions", sa.Column("auth_method", sa.String(24), nullable=True))
    op.add_column("orgs", sa.Column("session_idle_minutes", sa.Integer(), nullable=True))
    op.add_column("orgs", sa.Column("session_max_hours", sa.Integer(), nullable=True))
    op.add_column(
        "orgs",
        sa.Column("trust_idp_mfa", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "users", sa.Column("passkey_required_since", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("users", "passkey_required_since")
    op.drop_column("orgs", "trust_idp_mfa")
    op.drop_column("orgs", "session_max_hours")
    op.drop_column("orgs", "session_idle_minutes")
    op.drop_column("sessions", "auth_method")
    op.drop_column("sessions", "token_hash")
