from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user
from app.auth.security import create_access_token, hash_password, needs_rehash, verify_password
from app.config import Settings
from app.db.session import get_session
from app.errors import UnauthenticatedError
from app.models import User
from app.repositories import orgs as orgs_repo
from app.repositories import users as users_repo

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    full_name: str = ""


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


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
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MeOut:
    user = await users_repo.create_user(
        session, email=payload.email, password=payload.password, full_name=payload.full_name
    )
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
