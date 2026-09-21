from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.auth import LoginIn, TokenOut, _login
from app.auth.deps import get_current_user
from app.db.session import get_session
from app.errors import ConflictError, ValidationFailedError
from app.models import User
from app.rate_limit import enforce_rate_limit
from app.repositories import users as users_repo
from app.services import admin_invites as admin_invites_svc
from app.services import operators as operators_svc
from app.services import password_policy

router = APIRouter(prefix="/api/v1/auth/admin", tags=["auth"])


class AdminRegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)
    invite_token: str = Field(min_length=1, max_length=512)


class AdminInviteAcceptIn(BaseModel):
    invite_token: str = Field(min_length=1, max_length=512)


class AdminEnrollmentOut(BaseModel):
    id: UUID
    email: EmailStr
    full_name: str
    is_platform_operator: bool = True
    operator_role: str = "admin"
    requires_2fa_enrollment: bool


@router.post("/register", response_model=AdminEnrollmentOut, status_code=status.HTTP_201_CREATED)
async def admin_register(
    payload: AdminRegisterIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdminEnrollmentOut:
    settings = request.app.state.settings
    normalized_email = payload.email.strip().lower()

    # One call: enforce_rate_limit already checks BOTH the client IP and the identifier, so a
    # second call with an IP-shaped identifier would double-count the IP for this request.
    await enforce_rate_limit(request, f"admin_register:{normalized_email}")

    full_name = payload.full_name.strip()
    if not full_name:
        raise ValidationFailedError("Full name is required")

    try:
        invite = await admin_invites_svc.consume(
            session, token=payload.invite_token, email=normalized_email
        )

        existing = await users_repo.get_by_email(session, normalized_email)
        if existing is not None:
            raise ConflictError(
                "An account already exists for this email; sign in instead",
                code="existing_account_login_required",
            )

        await password_policy.check(settings, payload.password, email=normalized_email)

        user = await users_repo.create_user(
            session,
            email=normalized_email,
            password=payload.password,
            full_name=full_name,
        )

        invite.consumed_by = user.id

        await operators_svc.grant(session, email=normalized_email, role="admin")

        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError(
            "An account already exists for this email; sign in instead",
            code="existing_account_login_required",
        ) from exc
    except Exception:
        await session.rollback()
        raise

    return AdminEnrollmentOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_platform_operator=True,
        operator_role="admin",
        requires_2fa_enrollment=not user.has_second_factor,
    )


@router.post("/invites/accept", response_model=AdminEnrollmentOut)
async def admin_invite_accept(
    payload: AdminInviteAcceptIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_user)],
) -> AdminEnrollmentOut:
    if not user.is_active:
        raise ValidationFailedError("Account is not active")

    # One call: enforce_rate_limit already checks BOTH the client IP and the identifier, so a
    # second call with an IP-shaped identifier would double-count the IP for this request.
    await enforce_rate_limit(request, f"admin_invite_accept:{user.id}")

    try:
        await admin_invites_svc.consume(
            session,
            token=payload.invite_token,
            email=user.email,
            user_id=user.id,
        )

        await operators_svc.grant(session, email=user.email, role="admin")

        await session.commit()
    except Exception:
        await session.rollback()
        raise

    return AdminEnrollmentOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_platform_operator=True,
        operator_role="admin",
        requires_2fa_enrollment=not user.has_second_factor,
    )


@router.post("/login", response_model=TokenOut)
async def admin_login(
    payload: LoginIn,
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TokenOut:
    return await _login(payload, request, response, session, require_admin=True)
