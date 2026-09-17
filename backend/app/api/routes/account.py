"""P42 account lifecycle: password change / forgot / reset, recovery codes, identity-based
account recovery, and the user's own security activity.

Everything is under /api/v1/auth/ so the mandatory-2FA gate lets a user without a factor
reach it (they may be recovering).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import check_step_up, current_identity_session, get_current_user
from app.auth.security import decode_pending_2fa_token, hash_password, verify_password
from app.config import Settings
from app.db.session import get_session
from app.errors import UnauthenticatedError, ValidationFailedError
from app.models import AccountAuditEntry, KycStepUp, PasswordResetToken, SecurityAlert, User
from app.rate_limit import enforce_rate_limit
from app.repositories import users as users_repo
from app.services import (
    account_security,
    kyc_step_up,
    lockout,
    login_flow,
    password_policy,
    recovery_codes,
)
from app.services import identity as identity_svc

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------------------
# Password change
# --------------------------------------------------------------------------------------
class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=1, max_length=128)


@router.post("/password/change", status_code=204)
async def change_password(
    payload: PasswordChangeIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    settings: Settings = request.app.state.settings
    await enforce_rate_limit(request, f"password-change:{user.id}")
    await lockout.ensure_not_locked(session, user)
    if not verify_password(payload.current_password, user.hashed_password):
        await lockout.fail(
            session,
            settings,
            request,
            user,
            outcome="bad_password",
            error=UnauthenticatedError("Your current password is incorrect"),
        )
    if user.has_second_factor:
        await check_step_up(request, session, user, kind="recent_2fa", action="password_change")
    if payload.new_password == payload.current_password:
        raise ValidationFailedError("Choose a password you have not used for this account")
    await password_policy.check(settings, payload.new_password, email=user.email)

    user.hashed_password = hash_password(payload.new_password)
    user.password_changed_at = _now()
    live = await current_identity_session(request, session)
    revoked = await account_security.revoke_sessions(
        session, settings, user.id, revoked_by=user.id, keep_sid=live.id if live else None
    )
    account_security.audit(
        session,
        user.id,
        "password.changed",
        request=request,
        detail={"sessions_ended": len(revoked)},
    )
    if live is not None:
        from app.services import session_tokens

        session_tokens.rotate(response, settings, live)
    await session.commit()
    await account_security.mark_revoked(settings, revoked)
    await account_security.notify_now(
        settings,
        user.email,
        "Your password was changed",
        "The password for your account was just changed. Other signed-in devices were signed out.",
    )
    response.status_code = 204
    return response


# --------------------------------------------------------------------------------------
# Forgot / reset
# --------------------------------------------------------------------------------------
class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=1, max_length=128)


@router.post("/password/forgot", status_code=202)
async def forgot_password(
    payload: ForgotIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Always 202 with the same body, whether or not the account exists."""
    settings: Settings = request.app.state.settings
    await enforce_rate_limit(request, f"password-forgot:{payload.email.lower()}")
    user = await users_repo.get_by_email(session, payload.email)
    if user is not None and user.is_active:
        # One live link at a time: requesting again voids earlier ones.
        await session.execute(
            sa.update(PasswordResetToken)
            .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
            .values(used_at=_now())
            .execution_options(synchronize_session=False)
        )
        token = secrets.token_urlsafe(32)
        session.add(
            PasswordResetToken(
                id=uuid.uuid4(),
                user_id=user.id,
                token_hash=_token_hash(token),
                expires_at=_now() + timedelta(minutes=settings.password_reset_ttl_minutes),
                requested_ip=identity_svc.client_ip(request),
            )
        )
        account_security.audit(session, user.id, "password.reset_requested", request=request)
        await session.commit()
        link = f"{settings.public_web_url.rstrip('/')}/reset-password?token={token}"
        await account_security.notify_now(
            settings,
            user.email,
            "Reset your password",
            f"Someone asked to reset the password for {user.email}.\n\n"
            f"Reset it here (the link works once, for {settings.password_reset_ttl_minutes} "
            f"minutes):\n{link}\n\n"
            "You will still need your passkey or authenticator app to sign in afterwards.",
        )
    return {"status": "If that account exists, we sent a reset link."}


@router.post("/password/reset", status_code=204)
async def reset_password(
    payload: ResetIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Sets a new password and signs out every session. It deliberately does NOT sign the
    person in and never bypasses the second factor: email access alone must not be enough
    to take over an account."""
    settings: Settings = request.app.state.settings
    # P43: per token, never one global bucket that anyone could exhaust for everybody.
    await enforce_rate_limit(request, f"password-reset:{_token_hash(payload.token)[:24]}")
    row = (
        await session.execute(
            sa.select(PasswordResetToken).where(
                PasswordResetToken.token_hash == _token_hash(payload.token)
            )
        )
    ).scalar_one_or_none()
    expires = _aware(row.expires_at) if row is not None else None
    if row is None or row.used_at is not None or expires is None or expires <= _now():
        raise ValidationFailedError(
            "This reset link is invalid or has expired. Request a new one.", code="reset_invalid"
        )
    user = await users_repo.get_by_id(session, row.user_id)
    if user is None or not user.is_active:
        raise ValidationFailedError(
            "This reset link is invalid or has expired.", code="reset_invalid"
        )
    await password_policy.check(settings, payload.new_password, email=user.email)

    row.used_at = _now()
    user.hashed_password = hash_password(payload.new_password)
    user.password_changed_at = _now()
    revoked = await account_security.revoke_sessions(session, settings, user.id, revoked_by=user.id)
    account_security.audit(
        session, user.id, "password.reset", request=request, detail={"sessions_ended": len(revoked)}
    )
    await session.commit()
    await account_security.mark_revoked(settings, revoked)
    await account_security.notify_now(
        settings,
        user.email,
        "Your password was reset",
        "The password for your account was reset and every device was signed out.",
    )
    return Response(status_code=204)


# --------------------------------------------------------------------------------------
# Recovery codes
# --------------------------------------------------------------------------------------
class RecoveryLoginIn(BaseModel):
    pending_token: str
    code: str = Field(min_length=8, max_length=32)


@router.get("/recovery-codes")
async def recovery_codes_status(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    return {"remaining": await recovery_codes.remaining(session, user.id)}


@router.post("/recovery-codes")
async def generate_recovery_codes(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Shown once. Needs a second factor on the account and proof of it in this session."""
    settings: Settings = request.app.state.settings
    if not user.has_second_factor:
        raise ValidationFailedError("Set up a passkey or authenticator app first")
    await check_step_up(request, session, user, kind="recent_2fa", action="recovery_codes")
    codes = await recovery_codes.generate(session, user.id, settings.recovery_code_count)
    account_security.audit(session, user.id, "recovery_codes.generated", request=request)
    await session.commit()
    return {"codes": codes}


@router.post("/2fa/recovery")
async def login_with_recovery_code(
    payload: RecoveryLoginIn,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    settings: Settings = request.app.state.settings
    await enforce_rate_limit(request, f"recovery:{payload.pending_token}")
    user_id = decode_pending_2fa_token(
        payload.pending_token, settings.jwt_secret.get_secret_value()
    )
    user = await users_repo.get_by_id(session, user_id)
    if user is None or not user.is_active:
        raise UnauthenticatedError("Invalid verification session")
    await lockout.ensure_not_locked(session, user)
    if not await recovery_codes.consume(session, user.id, payload.code):
        await lockout.fail(
            session,
            settings,
            request,
            user,
            outcome="bad_2fa",
            error=UnauthenticatedError("That recovery code is not valid"),
            detail="recovery_code",
        )
    remaining = await recovery_codes.remaining(session, user.id)
    account_security.audit(
        session, user.id, "recovery_code.used", request=request, detail={"remaining": remaining}
    )
    session.add(
        SecurityAlert(
            id=uuid.uuid4(),
            kind="recovery_code_used",
            user_id=user.id,
            status="open",
            detail={
                "email": user.email,
                "remaining": remaining,
                "ip": identity_svc.client_ip(request),
            },
        )
    )
    token = await login_flow.complete_login(
        session,
        settings,
        request,
        user,
        second_factor=True,
        extra_flags=["recovery_code"],
        response=response,
        auth_method="recovery_code",
    )
    await account_security.notify_now(
        settings,
        user.email,
        "A recovery code was used to sign in",
        f"A recovery code was just used to sign in to your account. {remaining} codes remain.\n"
        "Add a new passkey or authenticator app and generate new recovery codes.",
    )
    return {"access_token": token, "token_type": "bearer", "recovery_codes_remaining": remaining}


# --------------------------------------------------------------------------------------
# Identity recovery (every factor lost)
# --------------------------------------------------------------------------------------
class PendingIn(BaseModel):
    pending_token: str
    return_url: str | None = Field(default=None, max_length=500)


class IdentityCompleteIn(BaseModel):
    pending_token: str
    step_up_id: uuid.UUID


async def _pending_user(session: AsyncSession, settings: Settings, token: str) -> User:
    user_id = decode_pending_2fa_token(token, settings.jwt_secret.get_secret_value())
    user = await users_repo.get_by_id(session, user_id)
    if user is None or not user.is_active:
        raise UnauthenticatedError("Invalid verification session")
    return user


@router.post("/recovery/identity/start")
async def start_identity_recovery(
    payload: PendingIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Password proven (pending token); every factor lost. Only people who passed an ID +
    selfie check during business verification can recover this way - the new selfie must
    match that same person."""
    settings: Settings = request.app.state.settings
    await enforce_rate_limit(request, f"identity-recovery:{payload.pending_token}")
    user = await _pending_user(session, settings, payload.pending_token)
    await lockout.ensure_not_locked(session, user)
    base = settings.public_web_url.rstrip("/")
    return_url = (
        payload.return_url
        if (payload.return_url or "").startswith(base + "/")
        else f"{base}/recover"
    )
    row, url = await kyc_step_up.start(
        session, settings, user, action="account_recovery", return_url=return_url
    )
    await session.commit()
    return {"step_up_id": str(row.id), "url": url}


@router.post("/recovery/identity/complete")
async def complete_identity_recovery(
    payload: IdentityCompleteIn,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    settings: Settings = request.app.state.settings
    await enforce_rate_limit(request, f"identity-recovery:{payload.pending_token}")
    user = await _pending_user(session, settings, payload.pending_token)
    await lockout.ensure_not_locked(session, user)
    row = await session.get(KycStepUp, payload.step_up_id)
    verified_at = _aware(row.verified_at) if row is not None else None
    fresh = verified_at is not None and _now() - verified_at <= timedelta(
        minutes=settings.step_up_selfie_minutes
    )
    if (
        row is None
        or row.user_id != user.id
        or row.action != "account_recovery"
        or row.status != "verified"
        or row.consumed_at is not None
        or not fresh
        or row.identity_hash not in await kyc_step_up.verified_identity_hashes(session, user.id)
    ):
        raise ValidationFailedError(
            "Your identity check is not complete or did not match. Try again.",
            code="identity_recovery_incomplete",
        )
    spent = await session.execute(
        sa.update(KycStepUp)
        .where(KycStepUp.id == row.id, KycStepUp.consumed_at.is_(None))
        .values(consumed_at=_now())
        .execution_options(synchronize_session=False)
    )
    if spent.rowcount != 1:
        raise ValidationFailedError(
            "Your identity check is not complete or did not match. Try again.",
            code="identity_recovery_incomplete",
        )
    row.consumed_at = _now()
    cleared = await account_security.clear_second_factors(session, user)
    user.step_up_blocked_until = _now() + timedelta(hours=settings.recovery_cooldown_hours)
    revoked = await account_security.revoke_sessions(session, settings, user.id, revoked_by=user.id)
    account_security.audit(
        session,
        user.id,
        "account.recovered_identity",
        request=request,
        detail={**cleared, "sessions_ended": len(revoked)},
    )
    session.add(
        SecurityAlert(
            id=uuid.uuid4(),
            kind="account_recovery",
            user_id=user.id,
            status="open",
            detail={"email": user.email, "ip": identity_svc.client_ip(request), **cleared},
        )
    )
    await session.flush()
    await account_security.mark_revoked(settings, revoked)
    token = await login_flow.complete_login(
        session,
        settings,
        request,
        user,
        second_factor=False,
        extra_flags=["identity_recovery"],
        response=response,
        auth_method="identity_recovery",
    )
    recipients = await login_flow.owner_emails_for_user(session, user)
    await account_security.notify_now(
        settings,
        recipients,
        "An account was recovered with an ID check",
        f"{user.email} lost their sign-in methods and recovered the account by passing an ID "
        f"and selfie check. Sensitive actions are blocked for {settings.recovery_cooldown_hours} "
        "hours while they set up a new passkey.",
    )
    return {"access_token": token, "token_type": "bearer"}


# --------------------------------------------------------------------------------------
# The user's own security activity
# --------------------------------------------------------------------------------------
@router.get("/activity")
async def account_activity(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict]:
    rows = (
        (
            await session.execute(
                sa.select(AccountAuditEntry)
                .where(AccountAuditEntry.user_id == user.id)
                .order_by(AccountAuditEntry.at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "action": r.action,
            "at": r.at.isoformat(),
            "ip": r.ip,
            "by_someone_else": r.actor_user_id not in (None, user.id),
            "detail": r.detail,
        }
        for r in rows
    ]


# --------------------------------------------------------------------------------------
# Sign out (P42 cookie sessions)
# --------------------------------------------------------------------------------------
@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Ends the current browser session and clears its cookies. Works even when the session
    has already expired, so the console can always get back to a clean signed-out state."""
    from app.models import Session as IdentitySession
    from app.services import session_cache, session_tokens

    settings: Settings = request.app.state.settings
    cookie = request.cookies.get(session_tokens.session_cookie_name(settings))
    parsed = session_tokens.parse(cookie)
    if parsed is not None:
        sid, secret = parsed
        row = await session.get(IdentitySession, sid)
        if (
            row is not None
            and row.revoked_at is None
            and session_tokens.secret_matches(row, secret)
        ):
            row.revoked_at = _now()
            row.revoked_by = row.user_id
            await session.commit()
            await session_cache.mark_revoked(settings, sid)
    session_tokens.clear_cookies(response, settings)
    response.status_code = 204
    return response
