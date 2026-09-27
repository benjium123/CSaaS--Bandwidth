"""Email codes on by default: every active, confirmed account gets email_2fa_enabled.

Data only. An account that ever turned email codes off (an `email_2fa.disabled` row in
account_audit_log) is left alone, as is one still waiting to confirm its address (that
account is switched on when it enters its confirmation code). Each flip writes an
`email_2fa.enabled` audit row with detail {"via": "migration_0075"}, so the change shows in
the account's activity and `downgrade` reverts exactly the accounts this migration touched.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

from app.db.types import GUID, PortableJSON

revision = "0075_email_2fa_default"
down_revision = "0074_yearly_billing"
branch_labels = None
depends_on = None

VIA = "migration_0075"

users = sa.table(
    "users",
    sa.column("id", GUID()),
    sa.column("is_active", sa.Boolean()),
    sa.column("email_2fa_enabled", sa.Boolean()),
    sa.column("email_verification_required", sa.Boolean()),
)
audit = sa.table(
    "account_audit_log",
    sa.column("id", GUID()),
    sa.column("user_id", GUID()),
    sa.column("actor_user_id", GUID()),
    sa.column("action", sa.String()),
    sa.column("at", sa.DateTime(timezone=True)),
    sa.column("detail", PortableJSON()),
)


def upgrade():
    conn = op.get_bind()
    now = datetime.now(timezone.utc)
    opted_out = sa.select(audit.c.user_id).where(audit.c.action == "email_2fa.disabled")
    ids = (
        conn.execute(
            sa.select(users.c.id).where(
                users.c.is_active.is_(True),
                users.c.email_2fa_enabled.is_(False),
                users.c.email_verification_required.is_(False),
                users.c.id.not_in(opted_out),
            )
        )
        .scalars()
        .all()
    )
    for uid in ids:
        conn.execute(sa.update(users).where(users.c.id == uid).values(email_2fa_enabled=True))
        conn.execute(
            sa.insert(audit).values(
                id=uuid.uuid4(),
                user_id=uid,
                actor_user_id=None,
                action="email_2fa.enabled",
                at=now,
                detail={"via": VIA},
            )
        )


def downgrade():
    conn = op.get_bind()
    rows = conn.execute(
        sa.select(audit.c.id, audit.c.user_id, audit.c.detail).where(
            audit.c.action == "email_2fa.enabled"
        )
    ).all()
    for audit_id, uid, detail in rows:
        if not isinstance(detail, dict) or detail.get("via") != VIA:
            continue
        conn.execute(sa.update(users).where(users.c.id == uid).values(email_2fa_enabled=False))
        conn.execute(sa.delete(audit).where(audit.c.id == audit_id))
