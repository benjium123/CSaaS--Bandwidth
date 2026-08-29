"""Request-scoped dependencies: who is calling, for which org, with what permissions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import (
    API_KEY_TOKEN_PREFIX,
    api_key_hash_matches,
    decode_access_token,
    parse_api_key_prefix,
)
from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.db.session import get_session
from app.errors import PermissionDeniedError, UnauthenticatedError, ValidationFailedError
from app.models import ApiKey, Org, OrgMembership, Role, User
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
    membership: OrgMembership | None
    role: Role
    session: AsyncSession
    #: Set when this context was authenticated by an API key (P13 DR-3/DR-11). The
    #: membership is None in that case; `role` is a TRANSIENT scope-limited Role (never
    #: session.add'ed) so require_permission works identically for both auth kinds.
    api_key: ApiKey | None = None

    @property
    def actor_user_id(self) -> uuid.UUID | None:
        """The human actor, or None for an API-key caller. Routes must use this instead
        of ``membership.user_id`` so key-authenticated requests cannot 500."""
        return self.membership.user_id if self.membership is not None else None


async def _org_context_from_api_key(
    token: str, session: AsyncSession
) -> OrgContext:
    """P13 DR-3. Key format ``csk_<prefix>_<secret>``; storage is hash-only; lookup by
    unique prefix then constant-time hash compare. 401 for any invalid/revoked/expired
    key — 403 is reserved for a VALID key missing a scope (require_permission)."""
    prefix = parse_api_key_prefix(token)
    if prefix is None:
        raise UnauthenticatedError("Malformed API key")
    # JUSTIFIED allow_unscoped: pre-tenant-resolution — the key row IS what resolves the
    # org, constrained to one exact unique prefix.
    row = (
        await session.execute(
            sa.select(ApiKey)
            .where(ApiKey.prefix == prefix)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if row is None or not api_key_hash_matches(token, row.key_hash):
        raise UnauthenticatedError("Invalid API key")
    if row.status != "active":
        raise UnauthenticatedError("This API key has been revoked")
    if row.expires_at is not None:
        now = datetime.now(timezone.utc)
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            raise UnauthenticatedError("This API key has expired")

    set_org_context(session, row.org_id)
    org = await session.get(Org, row.org_id)
    if org is None or not org.is_active:
        raise PermissionDeniedError("This organization is disabled")

    # Usage stamp at HOUR granularity with its own commit: a GET-only route never
    # commits the request session (P13 Opus finding), so a purely-read key would
    # otherwise never record a use. Committing every request would double writes for
    # machine traffic; once per hour per key is enough signal for "is this key alive".
    now = datetime.now(timezone.utc)
    last = row.last_used_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    row.last_used_at = now
    if last is None or (now - last) >= timedelta(hours=1):
        await session.commit()
        set_org_context(session, row.org_id)
        row = await session.get(ApiKey, row.id)

    # Defensive: scopes are validated at creation, but the wildcard must never work via
    # a key even if one sneaks into the column.
    scopes = [s for s in (row.scopes or []) if s != "*"]
    role = Role(id=uuid.uuid4(), org_id=row.org_id, name="api-key", permissions=scopes)
    return OrgContext(org=org, membership=None, role=role, session=session, api_key=row)


async def get_current_org(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
    x_org_id: Annotated[str | None, Header(alias="X-Org-Id")] = None,
) -> OrgContext:
    # P13 DR-11: an API key authenticates against the SAME org-scoped routes. The key is
    # org-bound, so X-Org-Id is optional — but when present it must agree.
    if creds is not None and creds.credentials.startswith(f"{API_KEY_TOKEN_PREFIX}_"):
        ctx = await _org_context_from_api_key(creds.credentials, session)
        if x_org_id and x_org_id != str(ctx.org.id):
            raise PermissionDeniedError("X-Org-Id does not match this API key's organization")
        return ctx

    user = await get_current_user(request, creds, session)
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
