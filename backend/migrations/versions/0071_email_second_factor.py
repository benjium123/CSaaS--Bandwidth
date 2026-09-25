"""A one-time code by email as a second factor. Additive only."""

import sqlalchemy as sa
from alembic import op

revision = "0071_email_second_factor"
down_revision = "0070_e911_signalwire"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("email_2fa_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("users", sa.Column("email_code_hash", sa.String(128), nullable=True))
    op.add_column("users", sa.Column("email_code_purpose", sa.String(16), nullable=True))
    op.add_column(
        "users", sa.Column("email_code_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "users",
        sa.Column("email_code_attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade():
    for column in (
        "email_code_attempts",
        "email_code_expires_at",
        "email_code_purpose",
        "email_code_hash",
        "email_2fa_enabled",
    ):
        op.drop_column("users", column)
