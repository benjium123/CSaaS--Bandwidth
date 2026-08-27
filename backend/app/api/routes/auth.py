from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user
from app.auth.security import (
    create_access_token,
    create_pending_2fa_token,
    hash_password,
    needs_rehash,
    verify_password,
)
from app.config import Settings
from app.db.session import get_session
from app.errors import UnauthenticatedError, ValidationFailedError
from app.models import User
from app.repositories import orgs as orgs_repo
from app.repositories import users as users_repo
from app.services import invites as invites_svc

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    full_name: str = ""
    #: Required unless this is the very first account on the instance.
    invite_token: str = ""


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str | None = None
    token_type: str = "bearer"
    # When 2FA is enabled the password step alone is NOT a login.
    requires_2fa: bool = False
    pending_token: str | None = None


class MembershipOut(BaseModel):
    org_id: uuid.UUID
    org_name: str
    org_slug: str
    role_name: str


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    memberships: list[MembershipOut]


@router.post("/register", response_model=MeOut, status_code=201)
async def register(
    payload: RegisterIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MeOut:
    """Registration is INVITE-ONLY once the instance has an owner.

    The single exception is first-run: while no account exists at all there is nobody who
    could issue an invitation, so the first registration is allowed and becomes the owner.
    That gate is a COUNT of users rather than a config flag - a flag can be left switched
    on by accident and silently reopen the instance months later; this condition flips
    itself the moment the first account exists and can never drift back.
    """
    settings: Settings = request.app.state.settings
    bootstrap = settings.allow_open_registration or not await invites_svc.instance_has_users(
        session
    )

    invite = None
    if not bootstrap:
        if not payload.invite_token:
            raise ValidationFailedError(
                "This instance is invite-only. Ask an administrator for an invitation."
            )
        invite = await invites_svc.find_redeemable(
            session, payload.invite_token, payload.email
        )

    user = await users_repo.create_user(
        session, email=payload.email, password=payload.password, full_name=payload.full_name
    )
    if invite is not None:
        await session.flush()
        await invites_svc.redeem(session, invite, user.id)
    await session.commit()
    return MeOut(id=user.id, email=user.email, full_name=user.full_name, memberships=[])


@router.post("/login", response_model=TokenOut)
async def login(
    payload: LoginIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenOut:
    settings: Settings = request.app.state.settings
    user = await users_repo.get_by_email(session, payload.email)

    # Same failure shape for unknown-email and bad-password. Verifying against a throwaway
    # hash when the user is missing keeps the timing profile similar, so login cannot be
    # used to enumerate which emails have accounts.
    if user is None:
        hash_password(payload.password)
        raise UnauthenticatedError("Incorrect email or password")
    if not verify_password(payload.password, user.hashed_password):
        raise UnauthenticatedError("Incorrect email or password")
    if not user.is_active:
        raise UnauthenticatedError("Incorrect email or password")

    if needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(payload.password)
        await session.commit()

    if user.totp_enabled:
        return TokenOut(
            requires_2fa=True,
            pending_token=create_pending_2fa_token(
                user.id, settings.jwt_secret.get_secret_value()
            ),
        )

    token = create_access_token(
        user.id,
        settings.jwt_secret.get_secret_value(),
        expire_hours=settings.jwt_expire_hours,
    )
    return TokenOut(access_token=token)


@router.get("/me", response_model=MeOut)
async def me(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MeOut:
    rows = await orgs_repo.list_memberships_for_user(session, user.id)
    return MeOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        memberships=[
            MembershipOut(
                org_id=org.id, org_name=org.name, org_slug=org.slug, role_name=role.name
            )
            for org, role in rows
        ],
    )
