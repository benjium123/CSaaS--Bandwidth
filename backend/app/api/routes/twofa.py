"""TOTP two-factor auth.

P0 deliberately parked this here: the console is the enrollment surface, and P2b is the
moment the login endpoint faces the public internet. It does not slip again.

The secret is Fernet-encrypted with ``CREDENTIAL_ENCRYPTION_KEY`` — the first real consumer
of that key. There is **no plaintext fallback branch**: without the key, enrollment answers
503. Branching secret storage is bug bait.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Annotated

import pyotp
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import check_step_up, current_identity_session, get_current_user
from app.auth.security import (
    decode_pending_2fa_token,
    decrypt_credential,
    encrypt_credential,
    verify_password,
)
from app.config import Settings
from app.db.session import get_session
from app.errors import (
    FeatureUnavailableError,
    UnauthenticatedError,
    ValidationFailedError,
)
from app.models import User
from app.rate_limit import enforce_rate_limit
from app.repositories import users as users_repo
from app.services import account_security, lockout, login_flow, second_factor, session_tokens

router = APIRouter(prefix="/api/v1/auth/2fa", tags=["auth"])

TOTP_STEP = 30
VALID_WINDOW = 1


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class VerifyIn(BaseModel):
    pending_token: str
    code: str = Field(min_length=6, max_length=8)


class PasswordIn(BaseModel):
    password: str


class DisableIn(CodeIn):
    password: str


class EnrollOut(BaseModel):
    secret: str
    provisioning_uri: str


def _fernet_key(settings: Settings) -> str:
    key = settings.credential_encryption_key.get_secret_value().strip()
    if not key:
        raise FeatureUnavailableError("Two-factor auth needs CREDENTIAL_ENCRYPTION_KEY to be set")
    return key


def _check_code(user: User, secret: str, code: str) -> int:
    """Verify a TOTP code and return its actual timestep, or raise."""
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    counter = now // TOTP_STEP
    for drift in range(-VALID_WINDOW, VALID_WINDOW + 1):
        step = counter + drift
        if not totp.verify(code, valid_window=0, for_time=step * TOTP_STEP):
            continue
        # Replay guard: comparing the actual step rejects reuse inside +/-1.
        if user.totp_last_used_step is not None and step <= user.totp_last_used_step:
            raise UnauthenticatedError("That code was already used")
        return step
    raise UnauthenticatedError("Invalid verification code")


@router.post("/enroll", response_model=EnrollOut)
async def enroll(
    payload: PasswordIn,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EnrollOut:
    if not verify_password(payload.password, user.hashed_password):
        raise UnauthenticatedError("Incorrect password")
    # P43: a passkey user adding an authenticator app proves the passkey first.
    if user.has_second_factor:
        await check_step_up(request, session, user, kind="recent_2fa", action="totp_change")
    settings: Settings = request.app.state.settings
    key = _fernet_key(settings)
    if user.totp_enabled:
        raise ValidationFailedError("Two-factor auth is already enabled")

    secret = pyotp.random_base32()
    user.totp_secret = encrypt_credential(secret, key)
    user.totp_enabled = False
    user.totp_last_used_step = None
    await session.commit()

    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=settings.app_name)
    return EnrollOut(secret=secret, provisioning_uri=uri)


@router.post("/activate")
async def activate(
    payload: CodeIn,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    settings: Settings = request.app.state.settings
    key = _fernet_key(settings)
    if not user.totp_secret:
        raise ValidationFailedError("Start enrollment first")

    secret = decrypt_credential(user.totp_secret, key)
    step = _check_code(user, secret, payload.code)
    had_factor = bool(user.has_second_factor)
    user.totp_enabled = True
    user.totp_last_used_step = step
    # P41: activating proves possession of the factor, so the enrolling session counts as
    # second-factor-verified from here on.
    row = await current_identity_session(request, session)
    if row is not None and not had_factor:
        row.second_factor_at = datetime.now(timezone.utc)
    account_security.audit(session, user.id, "totp.enabled", request=request)
    await session.commit()
    await account_security.notify_now(
        settings,
        user.email,
        "Authenticator app added",
        "An authenticator app was added to your account.",
    )
    return {"totp_enabled": True}


@router.post("/verify")
async def verify(
    payload: VerifyIn,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Exchange a pending-2FA token + code for a real access token.

    P25: THIS is where a 2FA login becomes a login - routes/auth.py deliberately creates
    no Session and logs no ``ok`` event for a TOTP-enabled user, because the pending
    token is not yet a sign-in. The Session row and the login event are minted here.
    """
    settings: Settings = request.app.state.settings
    await enforce_rate_limit(request, f"totp:{payload.pending_token}")
    key = _fernet_key(settings)

    user_id = decode_pending_2fa_token(
        payload.pending_token, settings.jwt_secret.get_secret_value()
    )
    user = await users_repo.get_by_id(session, user_id)
    if user is None or not user.totp_enabled or not user.totp_secret:
        raise UnauthenticatedError("Invalid verification session")

    await lockout.ensure_not_locked(session, user)
    secret = decrypt_credential(user.totp_secret, key)
    try:
        step = _check_code(user, secret, payload.code)
    except UnauthenticatedError as exc:
        # A wrong or replayed second factor is a security event in its own right, and it
        # is committed even though the request fails. P42: it also counts toward lockout.
        await lockout.fail(session, settings, request, user, outcome="bad_2fa", error=exc)

    user.totp_last_used_step = step

    token = await login_flow.complete_login(
        session,
        settings,
        request,
        user,
        second_factor=True,
        response=response,
        auth_method="password_totp",
    )
    return {"access_token": token, "token_type": "bearer"}


@router.post("/step-up")
async def step_up(
    payload: CodeIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """P41: re-prove the authenticator app inside an existing session (step-up)."""
    settings: Settings = request.app.state.settings
    await enforce_rate_limit(request, f"totp-step-up:{user.id}")
    key = _fernet_key(settings)
    if not user.totp_enabled or not user.totp_secret:
        raise ValidationFailedError("Two-factor auth is not enabled")
    row = await current_identity_session(request, session)
    if row is None:
        raise UnauthenticatedError("Sign in again to continue")
    secret = decrypt_credential(user.totp_secret, key)
    # P43: wrong step-up codes count toward the account lockout, like sign-in codes do.
    await lockout.ensure_not_locked(session, user)
    try:
        step = _check_code(user, secret, payload.code)
    except UnauthenticatedError as exc:
        await lockout.fail(session, settings, request, user, outcome="bad_2fa", error=exc)
        raise
    user.totp_last_used_step = step
    row.second_factor_at = datetime.now(timezone.utc)
    session_tokens.rotate(response, settings, row)
    await session.commit()
    return {"ok": True}


@router.post("/disable")
async def disable(
    payload: DisableIn,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    if not verify_password(payload.password, user.hashed_password):
        raise UnauthenticatedError("Incorrect password")
    settings: Settings = request.app.state.settings
    key = _fernet_key(settings)
    if not user.totp_enabled or not user.totp_secret:
        raise ValidationFailedError("Two-factor auth is not enabled")

    # P41: only a PRIVILEGED account (owner/admin in any org) is obliged to keep a factor.
    # Ordinary staff may turn the authenticator app off. requires_second_factor, not
    # must_enrol: the question is the obligation, not whether a factor exists right now.
    if not user.has_passkey and await second_factor.requires_second_factor(
        session, settings, user
    ):
        raise ValidationFailedError(
            "Admins and owners must keep a second factor. Add a passkey before turning off "
            "the authenticator app.",
            code="last_second_factor",
        )

    secret = decrypt_credential(user.totp_secret, key)
    _check_code(user, secret, payload.code)
    user.totp_enabled = False
    user.totp_secret = None
    user.totp_last_used_step = None
    account_security.audit(session, user.id, "totp.disabled", request=request)
    await session.commit()
    await account_security.notify_now(
        settings,
        user.email,
        "Authenticator app removed",
        "The authenticator app was removed from your account.",
    )
    return {"totp_enabled": False}
