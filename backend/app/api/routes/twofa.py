"""TOTP two-factor auth.

P0 deliberately parked this here: the console is the enrollment surface, and P2b is the
moment the login endpoint faces the public internet. It does not slip again.

The secret is Fernet-encrypted with ``CREDENTIAL_ENCRYPTION_KEY`` — the first real consumer
of that key. There is **no plaintext fallback branch**: without the key, enrollment answers
503. Branching secret storage is bug bait.
"""

from __future__ import annotations

import time
from typing import Annotated

import pyotp
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user
from app.auth.security import (
    create_access_token,
    decode_pending_2fa_token,
    decrypt_credential,
    encrypt_credential,
)
from app.config import Settings
from app.db.session import get_session
from app.errors import (
    FeatureUnavailableError,
    UnauthenticatedError,
    ValidationFailedError,
)
from app.models import User
from app.repositories import users as users_repo

router = APIRouter(prefix="/api/v1/auth/2fa", tags=["auth"])

TOTP_STEP = 30
VALID_WINDOW = 1


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class VerifyIn(BaseModel):
    pending_token: str
    code: str = Field(min_length=6, max_length=8)


class EnrollOut(BaseModel):
    secret: str
    provisioning_uri: str


def _fernet_key(settings: Settings) -> str:
    key = settings.credential_encryption_key.get_secret_value().strip()
    if not key:
        raise FeatureUnavailableError(
            "Two-factor auth needs CREDENTIAL_ENCRYPTION_KEY to be set"
        )
    return key


def _check_code(user: User, secret: str, code: str) -> int:
    """Verify a TOTP code and return its timestep, or raise."""
    totp = pyotp.TOTP(secret)
    if not totp.verify(code, valid_window=VALID_WINDOW):
        raise UnauthenticatedError("Invalid verification code")
    step = int(time.time()) // TOTP_STEP
    # Replay guard: a code already accepted cannot be reused inside its window.
    if user.totp_last_used_step is not None and step <= user.totp_last_used_step:
        raise UnauthenticatedError("That code was already used")
    return step


@router.post("/enroll", response_model=EnrollOut)
async def enroll(
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> EnrollOut:
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
    user.totp_enabled = True
    user.totp_last_used_step = step
    await session.commit()
    return {"totp_enabled": True}


@router.post("/verify")
async def verify(
    payload: VerifyIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    """Exchange a pending-2FA token + code for a real access token."""
    settings: Settings = request.app.state.settings
    key = _fernet_key(settings)

    user_id = decode_pending_2fa_token(
        payload.pending_token, settings.jwt_secret.get_secret_value()
    )
    user = await users_repo.get_by_id(session, user_id)
    if user is None or not user.totp_enabled or not user.totp_secret:
        raise UnauthenticatedError("Invalid verification session")

    secret = decrypt_credential(user.totp_secret, key)
    step = _check_code(user, secret, payload.code)
    user.totp_last_used_step = step
    await session.commit()

    token = create_access_token(
        user.id, settings.jwt_secret.get_secret_value(), expire_hours=settings.jwt_expire_hours
    )
    return {"access_token": token, "token_type": "bearer"}


@router.post("/disable")
async def disable(
    payload: CodeIn,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    settings: Settings = request.app.state.settings
    key = _fernet_key(settings)
    if not user.totp_enabled or not user.totp_secret:
        raise ValidationFailedError("Two-factor auth is not enabled")

    secret = decrypt_credential(user.totp_secret, key)
    _check_code(user, secret, payload.code)
    user.totp_enabled = False
    user.totp_secret = None
    user.totp_last_used_step = None
    await session.commit()
    return {"totp_enabled": False}
