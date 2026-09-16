"""P41 passkeys: register, list, remove, sign in with, and step up with a passkey.

Everything lives under /api/v1/auth/, which the mandatory-2FA gate exempts - a user with no
factor yet must be able to register their first passkey.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_identity_session, get_current_user
from app.auth.security import decode_pending_2fa_token
from app.config import Settings
from app.db.session import get_session
from app.errors import UnauthenticatedError
from app.models import User
from app.rate_limit import enforce_rate_limit
from app.repositories import users as users_repo
from app.services import account_security, lockout, login_flow, session_tokens
from app.services import passkeys as passkeys_svc

router = APIRouter(prefix="/api/v1/auth/passkeys", tags=["auth"])


class OptionsOut(BaseModel):
    challenge_id: uuid.UUID
    options: dict


class RegisterIn(BaseModel):
    challenge_id: uuid.UUID
    credential: dict
    name: str = Field(default="Passkey", max_length=64)


class PasskeyOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None


class PendingIn(BaseModel):
    pending_token: str


class LoginVerifyIn(BaseModel):
    pending_token: str
    challenge_id: uuid.UUID
    credential: dict


class StepUpVerifyIn(BaseModel):
    challenge_id: uuid.UUID
    credential: dict


def _out(row) -> PasskeyOut:
    return PasskeyOut(
        id=row.id, name=row.name, created_at=row.created_at, last_used_at=row.last_used_at
    )


@router.get("", response_model=list[PasskeyOut])
async def list_passkeys(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[PasskeyOut]:
    return [_out(p) for p in await passkeys_svc.list_for_user(session, user.id)]


@router.post("/register/options", response_model=OptionsOut)
async def register_options(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OptionsOut:
    settings: Settings = request.app.state.settings
    challenge_id, options = await passkeys_svc.registration_options(session, settings, user)
    await session.commit()
    return OptionsOut(challenge_id=challenge_id, options=options)


@router.post("/register", response_model=PasskeyOut, status_code=201)
async def register(
    payload: RegisterIn,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PasskeyOut:
    settings: Settings = request.app.state.settings
    try:
        row = await passkeys_svc.register(
            session,
            settings,
            user,
            challenge_id=payload.challenge_id,
            credential=payload.credential,
            name=payload.name,
        )
    except Exception:
        # The consumed challenge must stay consumed even when verification fails.
        await session.commit()
        raise
    # Registering proves possession of the new factor, like activating TOTP does.
    live = await current_identity_session(request, session)
    if live is not None:
        live.second_factor_at = datetime.now(timezone.utc)
    account_security.audit(
        session, user.id, "passkey.added", request=request, detail={"name": row.name}
    )
    await session.commit()
    await account_security.notify_now(
        settings,
        user.email,
        "New passkey added",
        f'A passkey named "{row.name}" was added to your account.',
    )
    return _out(row)


@router.delete("/{passkey_id}", status_code=204)
async def delete_passkey(
    passkey_id: uuid.UUID,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    settings: Settings = request.app.state.settings
    await passkeys_svc.delete(session, settings, user, passkey_id)
    account_security.audit(session, user.id, "passkey.removed", request=request)
    await session.commit()
    await account_security.notify_now(
        settings, user.email, "Passkey removed", "A passkey was removed from your account."
    )
    return Response(status_code=204)


async def _pending_user(session: AsyncSession, settings: Settings, token: str) -> User:
    user_id = decode_pending_2fa_token(token, settings.jwt_secret.get_secret_value())
    user = await users_repo.get_by_id(session, user_id)
    if user is None or not user.is_active or not user.has_passkey:
        raise UnauthenticatedError("Invalid verification session")
    return user


@router.post("/login/options", response_model=OptionsOut)
async def login_options(
    payload: PendingIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OptionsOut:
    settings: Settings = request.app.state.settings
    await enforce_rate_limit(request, f"passkey:{payload.pending_token}")
    user = await _pending_user(session, settings, payload.pending_token)
    challenge_id, options = await passkeys_svc.authentication_options(
        session, settings, user, purpose="login"
    )
    await session.commit()
    return OptionsOut(challenge_id=challenge_id, options=options)


@router.post("/login/verify")
async def login_verify(
    payload: LoginVerifyIn,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Second factor by passkey: exchange the pending token + assertion for a session."""
    settings: Settings = request.app.state.settings
    await enforce_rate_limit(request, f"passkey:{payload.pending_token}")
    user = await _pending_user(session, settings, payload.pending_token)
    await lockout.ensure_not_locked(session, user)
    try:
        await passkeys_svc.authenticate(
            session,
            settings,
            user,
            challenge_id=payload.challenge_id,
            credential=payload.credential,
            purpose="login",
        )
    except UnauthenticatedError as exc:
        await lockout.fail(session, settings, request, user, outcome="bad_2fa", error=exc)
    token = await login_flow.complete_login(
        session,
        settings,
        request,
        user,
        second_factor=True,
        response=response,
        auth_method="passkey",
    )
    return {"access_token": token, "token_type": "bearer"}


@router.post("/step-up/options", response_model=OptionsOut)
async def step_up_options(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OptionsOut:
    settings: Settings = request.app.state.settings
    challenge_id, options = await passkeys_svc.authentication_options(
        session, settings, user, purpose="step_up"
    )
    await session.commit()
    return OptionsOut(challenge_id=challenge_id, options=options)


@router.post("/step-up/verify")
async def step_up_verify(
    payload: StepUpVerifyIn,
    request: Request,
    response: Response,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    settings: Settings = request.app.state.settings
    await enforce_rate_limit(request, f"passkey-step-up:{user.id}")
    live = await current_identity_session(request, session)
    if live is None:
        raise UnauthenticatedError("Sign in again to continue")
    try:
        await passkeys_svc.authenticate(
            session,
            settings,
            user,
            challenge_id=payload.challenge_id,
            credential=payload.credential,
            purpose="step_up",
        )
    except UnauthenticatedError:
        await session.commit()
        raise
    live.second_factor_at = datetime.now(timezone.utc)
    # P42: proving a passkey inside the session makes it a passkey session (what privileged
    # roles need), without signing out.
    live.auth_method = "passkey"
    session_tokens.rotate(response, settings, live)
    await session.commit()
    return {"ok": True}
