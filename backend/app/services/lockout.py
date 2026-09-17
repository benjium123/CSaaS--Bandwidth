"""P42 progressive account lockout.

Failures are counted from ``login_events`` (``bad_password`` and ``bad_2fa`` for the user)
in the last LOCKOUT_WINDOW_MINUTES since the last lock ended - the database is shared by
every worker, so the count is correct without Redis. Crossing LOCKOUT_THRESHOLD locks the
account for LOCKOUT_BASE_MINUTES, doubling on each repeat within 24 h (cap 24 h).

What the caller reveals: a wrong password on a locked account gets the usual generic 401 -
an attacker guessing learns nothing. Only the CORRECT password on a locked account is told
the account is temporarily locked, and the owner is emailed when a lock starts.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import CsaasError
from app.models import AccountLockout, LoginEvent, SecurityAlert, User
from app.services import account_security

FAILURE_OUTCOMES = ("bad_password", "bad_2fa")
MAX_LOCK = timedelta(hours=24)


class AccountLockedError(CsaasError):
    code = "account_locked"
    http_status = 423
    message = "Too many failed attempts. Try again later or reset your password."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _row(session: AsyncSession, user_id: uuid.UUID) -> AccountLockout | None:
    return (
        await session.execute(sa.select(AccountLockout).where(AccountLockout.user_id == user_id))
    ).scalar_one_or_none()


async def locked_until(
    session: AsyncSession, user_id: uuid.UUID, *, now: datetime | None = None
) -> datetime | None:
    row = await _row(session, user_id)
    until = _aware(row.locked_until) if row is not None else None
    now = now or _now()
    return until if until is not None and until > now else None


async def ensure_not_locked(session: AsyncSession, user: User) -> None:
    until = await locked_until(session, user.id)
    if until is not None:
        minutes = max(1, int((until - _now()).total_seconds() // 60) + 1)
        raise AccountLockedError(
            f"Too many failed attempts. Try again in {minutes} minutes or reset your password."
        )


async def register_failure(
    session: AsyncSession,
    settings: Settings,
    user: User,
    request: Request | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Call AFTER the failed LoginEvent is added. Returns True when this failure locked the
    account. Caller commits."""
    now = now or _now()
    await session.flush()
    row = await _row(session, user.id)
    window_start = now - timedelta(minutes=settings.lockout_window_minutes)
    last_end = _aware(row.locked_until) if row is not None else None
    # Count from the end of the last lock - but never from a future moment while a lock is
    # still running, or failures during the lock would be ignored.
    if last_end is not None and last_end > now:
        last_end = None
    since = max(window_start, last_end) if last_end is not None else window_start
    failures = (
        await session.execute(
            sa.select(sa.func.count(LoginEvent.id)).where(
                LoginEvent.user_id == user.id,
                LoginEvent.outcome.in_(FAILURE_OUTCOMES),
                LoginEvent.at >= since,
            )
        )
    ).scalar_one()
    if failures < settings.lockout_threshold:
        return False

    if row is None:
        row = AccountLockout(id=uuid.uuid4(), user_id=user.id, level=0)
        session.add(row)
    last_locked = _aware(row.last_locked_at)
    if last_locked is None or now - last_locked > timedelta(hours=24):
        row.level = 0
    duration = min(timedelta(minutes=settings.lockout_base_minutes * (2**row.level)), MAX_LOCK)
    row.level += 1
    row.last_locked_at = now
    row.locked_until = now + duration
    account_security.audit(
        session,
        user.id,
        "account.locked",
        request=request,
        detail={"minutes": int(duration.total_seconds() // 60), "failures": failures},
    )
    session.add(
        SecurityAlert(
            id=uuid.uuid4(),
            kind="account_locked",
            user_id=user.id,
            status="open",
            detail={
                "email": user.email,
                "failures": failures,
                "minutes": int(duration.total_seconds() // 60),
            },
        )
    )
    return True


async def unlock(
    session: AsyncSession, user_id: uuid.UUID, *, actor_user_id: uuid.UUID, request: Request | None
) -> None:
    row = await _row(session, user_id)
    if row is not None:
        row.locked_until = None
        row.level = 0
    account_security.audit(
        session, user_id, "account.unlocked", actor_user_id=actor_user_id, request=request
    )


LOCK_EMAIL = (
    "Your account was locked for {minutes} minutes after too many failed sign-in attempts.\n\n"
    "If you were trying to sign in, wait and try again, or reset your password."
)


async def fail(
    session: AsyncSession,
    settings: Settings,
    request: Request,
    user: User,
    *,
    outcome: str,
    error: Exception,
    detail: str | None = None,
) -> None:
    """Record a failed attempt for a KNOWN user, maybe lock, commit, email, then raise."""
    from app.services import identity as identity_svc

    identity_svc.record_login_event(
        session, email=user.email, outcome=outcome, user_id=user.id, request=request, detail=detail
    )
    locked = await register_failure(session, settings, user, request)
    await session.commit()
    if locked:
        until = await locked_until(session, user.id)
        minutes = (
            int((until - _now()).total_seconds() // 60) + 1
            if until
            else settings.lockout_base_minutes
        )
        await account_security.notify_now(
            settings,
            user.email,
            "Your account was temporarily locked",
            LOCK_EMAIL.format(minutes=minutes),
        )
    raise error
