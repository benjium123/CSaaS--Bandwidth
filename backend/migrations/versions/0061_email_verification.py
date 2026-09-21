"""Email ownership confirmation for new registrations; preserve existing access."""

import sqlalchemy as sa
from alembic import op

revision = "0061_email_verification"
down_revision = "0060_admin_invites"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column(
            "email_verification_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    for name in (
        "email_verified_at",
        "email_verification_expires_at",
        "email_verification_sent_at",
    ):
        op.add_column("users", sa.Column(name, sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("email_verification_hash", sa.String(64)))
    op.create_index(
        "ix_users_email_verification_hash", "users", ["email_verification_hash"], unique=True
    )


def downgrade():
    op.drop_index("ix_users_email_verification_hash", table_name="users")
    for name in (
        "email_verification_required",
        "email_verified_at",
        "email_verification_expires_at",
        "email_verification_sent_at",
        "email_verification_hash",
    ):
        op.drop_column("users", name)
