"""H4 break-glass: temporary admin access granted from the server, loudly.

Run ONLY from a shell on the server (scripts/break_glass.py) - there is no API for it, like
scripts/make_operator.py. The grant ends by itself (operators.get_active), is written to
operator_audit_log (role "break_glass"), opens a SecurityAlert for review and emails every
active admin operator, so an emergency grant can never be quiet.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import OperatorAuditEntry, PlatformOperator, SecurityAlert, User
from app.services import mailer
from app.services import operators as operators_svc


async def admin_emails(session: AsyncSession) -> list[str]:
    now = datetime.now(timezone.utc)
    rows = (
        await session.execute(
            sa.select(User.email)
            .join(PlatformOperator, PlatformOperator.user_id == User.id)
            .where(
                PlatformOperator.is_active.is_(True),
                PlatformOperator.role == "admin",
                sa.or_(PlatformOperator.expires_at.is_(None), PlatformOperator.expires_at > now),
            )
        )
    ).scalars().all()
    return sorted({e for e in rows if e})


async def grant(
    session: AsyncSession,
    settings: Settings,
    *,
    email: str,
    reason: str,
    minutes: int = 60,
    granted_by: str = "server shell",
) -> PlatformOperator:
    """Grant, audit, alert and notify; commits."""
    notify = await admin_emails(session)  # before the grant: the new admin is not "told"
    row = await operators_svc.break_glass(session, email=email, reason=reason, minutes=minutes)
    user = await session.get(User, row.user_id)
    now = datetime.now(timezone.utc)
    detail = {
        "email": user.email if user else email,
        "reason": reason.strip(),
        "expires_at": row.expires_at.isoformat(),
        "granted_by": granted_by,
    }
    session.add(
        OperatorAuditEntry(
            id=uuid.uuid4(),
            at=now,
            operator_user_id=row.user_id,
            operator_email=detail["email"],
            operator_role="break_glass",
            method="CLI",
            route="scripts/break_glass.py",
            path_params={"minutes": str(minutes)},
            org_id=None,
            status_code=200,
            reason=detail["reason"],
            ip=None,
            user_agent=granted_by[:255],
        )
    )
    session.add(
        SecurityAlert(id=uuid.uuid4(), kind="break_glass", user_id=row.user_id, detail=detail)
    )
    await session.commit()
    if notify:
        await mailer.send(
            settings,
            notify,
            f"Break-glass admin access granted on {settings.app_name}",
            (
                f"{detail['email']} was given temporary ADMIN operator access from the server.\n\n"
                f"Reason: {detail['reason']}\n"
                f"Ends: {detail['expires_at']} (UTC)\n"
                f"Granted by: {granted_by}\n\n"
                "If you did not expect this, revoke it now: "
                f"python scripts/make_operator.py revoke {detail['email']}"
            ),
        )
    return row
