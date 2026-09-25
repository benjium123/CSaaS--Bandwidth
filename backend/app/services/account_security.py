"""P42 account security building blocks shared by passwords, recovery, lockout and admin
resets: account audit, session revocation, factor reset, security notices.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import (
    ACCOUNT_AUDIT_ACTIONS,
    AccountAuditEntry,
    RecoveryCode,
    User,
    UserPasskey,
)
from app.services import email_code, mailer, session_cache
from app.services import identity as identity_svc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def audit(
    session: AsyncSession,
    user_id: uuid.UUID,
    action: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    request: Request | None = None,
    detail: dict | None = None,
) -> AccountAuditEntry:
    if action not in ACCOUNT_AUDIT_ACTIONS:
        raise ValueError(f"Unknown account audit action: {action}")
    row = AccountAuditEntry(
        id=uuid.uuid4(),
        user_id=user_id,
        actor_user_id=actor_user_id if actor_user_id is not None else user_id,
        action=action,
        at=_now(),
        ip=identity_svc.client_ip(request) if request is not None else None,
        user_agent=identity_svc.client_user_agent(request) if request is not None else None,
        detail=detail,
    )
    session.add(row)
    return row


async def revoke_sessions(
    session: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID,
    *,
    revoked_by: uuid.UUID,
    keep_sid: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """Revoke in the caller's transaction; returns ids. Call ``mark_revoked`` after commit."""
    return await identity_svc.revoke_all_for_user(
        session, user_id, revoked_by=revoked_by, keep_sid=keep_sid
    )


async def mark_revoked(settings: Settings, ids: list[uuid.UUID]) -> None:
    for sid in ids:
        await session_cache.mark_revoked(settings, sid)


async def clear_second_factors(session: AsyncSession, user: User) -> dict:
    """Remove the authenticator app, email codes, every passkey and every recovery code."""
    passkeys = await session.execute(sa.delete(UserPasskey).where(UserPasskey.user_id == user.id))
    await session.execute(sa.delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    had_totp = bool(user.totp_enabled)
    user.totp_enabled = False
    user.totp_secret = None
    user.totp_last_used_step = None
    user.has_passkey = False
    user.email_2fa_enabled = False
    email_code.clear(user)
    return {"totp_removed": had_totp, "passkeys_removed": passkeys.rowcount or 0}


async def notify_now(settings: Settings, to: list[str] | str, subject: str, body: str) -> None:
    recipients = [to] if isinstance(to, str) else to
    footer = "\n\nIf this wasn't you, reset your password and contact support immediately."
    await mailer.send(settings, recipients, f"{settings.app_name}: {subject}", body + footer)
