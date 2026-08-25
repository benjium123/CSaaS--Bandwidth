"""Request-scoped dependencies: who is calling, for which org, with what permissions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import decode_access_token
from app.config import Settings
from app.db.base import set_org_context
from app.db.session import get_session
from app.errors import PermissionDeniedError, UnauthenticatedError, ValidationFailedError
from app.models import Org, OrgMembership, Role, User
from app.repositories import orgs as orgs_repo
from app.repositories import users as users_repo

bearer_scheme = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_current_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    if creds is None or not creds.credentials:
        raise UnauthenticatedError("Missing bearer token")
    settings: Settings = request.app.state.settings
    user_id = decode_access_token(creds.credentials, settings.jwt_secret.get_secret_value())
    user = await users_repo.get_by_id(session, user_id)
    if user is None:
        raise UnauthenticatedError("Invalid or expired token")
    if not user.is_active:
        raise PermissionDeniedError("This account is disabled")
    return user


@dataclass
class OrgContext:
    org: Org
    membership: OrgMembership
    role: Role
    session: AsyncSession


async def get_current_org(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    x_org_id: Annotated[str | None, Header(alias="X-Org-Id")] = None,
) -> OrgContext:
    if not x_org_id:
        raise ValidationFailedError("X-Org-Id header is required for org-scoped routes")
    try:
        org_id = uuid.UUID(x_org_id)
    except (ValueError, AttributeError) as exc:
        raise ValidationFailedError("X-Org-Id is not a valid UUID") from exc

    found = await orgs_repo.get_membership(session, org_id=org_id, user_id=user.id)
    if found is None:
        # Deliberately the same 403 whether the org does not exist or the user simply is
        # not a member — do not let callers probe for which orgs exist.
        raise PermissionDeniedError("You are not a member of this organization")

    org, membership, role = found
    if not org.is_active:
        raise PermissionDeniedError("This organization is disabled")

    set_org_context(session, org.id)
    return OrgContext(org=org, membership=membership, role=role, session=session)


def require_permission(permission: str):
    """Dependency factory. Owner's ``*`` short-circuits every check."""

    async def _check(ctx: Annotated[OrgContext, Depends(get_current_org)]) -> OrgContext:
        if not ctx.role.grants(permission):
            raise PermissionDeniedError(f"Requires permission: {permission}")
        return ctx

    return _check
