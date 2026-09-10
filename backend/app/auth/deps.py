"""Request-scoped dependencies: who is calling, for which org, with what permissions."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
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
from app.providers import registry_org
from app.rate_limit import enforce_rate_limit
from app.repositories import orgs as orgs_repo
from app.repositories import users as users_repo
from app.services import credentials as credential_svc
from app.services import identity as identity_svc
from app.services import session_cache

bearer_scheme = HTTPBearer(auto_error=False)

#: Paths a 2FA-locked human user may still reach, kept as NARROW as possible: enough to
#: enrol a second factor and to inspect/ revoke their own sessions, and nothing else.
#: /api/v1/me/ as a whole is deliberately NOT exempt - /api/v1/me/capabilities serves org
#: data and is exactly the kind of route the policy exists to gate. Nor is
#: /api/v1/orgs/current/security: the org cannot switch the policy off from inside a
#: locked-out session (see the enable-time guard in api/routes/identity.py, which is what
#: makes that safe rather than a lockout).
_2FA_EXEMPT_PATH_PREFIXES = frozenset(
    {"/api/v1/auth/", "/api/v1/me/sessions", "/api/v1/me/login-events"}
)


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
    user_id, sid = decode_access_token(creds.credentials, settings.jwt_secret.get_secret_value())

    if sid is not None:
        request.state.session_id = sid
        cached_revoked = await session_cache.is_revoked(settings, sid)
        if cached_revoked is True:
            raise UnauthenticatedError("Invalid or expired token")
        if cached_revoked is None:
            live = await identity_svc.get_live_session(session, sid)
            if live is None:
                await session_cache.remember(settings, sid, True)
                raise UnauthenticatedError("Invalid or expired token")
            await session_cache.remember(settings, sid, False)

    user = await users_repo.get_by_id(session, user_id)
    if user is None:
        raise UnauthenticatedError("Invalid or expired token")
    if not user.is_active:
        raise UnauthenticatedError("Invalid or expired token")
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
    token: str, session: AsyncSession, request: Request
) -> OrgContext:
    """P13 DR-3. Key format ``csk_<prefix>_<secret>``; storage is hash-only; lookup by
    unique prefix then constant-time hash compare. 401 for any invalid/revoked/expired
    key — 403 is reserved for a VALID key missing a scope (require_permission)."""
    prefix = parse_api_key_prefix(token)
    if prefix is None:
        await enforce_rate_limit(request, "apikey:malformed")
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
        await enforce_rate_limit(request, f"apikey:{prefix}")
        raise UnauthenticatedError("Invalid API key")
    if row.status != "active":
        await enforce_rate_limit(request, f"apikey:{prefix}")
        raise UnauthenticatedError("This API key has been revoked")
    if row.expires_at is not None:
        now = datetime.now(timezone.utc)
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            await enforce_rate_limit(request, f"apikey:{prefix}")
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
) -> AsyncIterator[OrgContext]:
    # P13 DR-11: an API key authenticates against the SAME org-scoped routes. The key is
    # org-bound, so X-Org-Id is optional — but when present it must agree.
    #: Bound only on the human path; stays None for an API key. Declared up front so the
    #: P25 enforcement block below never reads a conditionally-bound name.
    user: User | None = None
    if creds is not None and creds.credentials.startswith(f"{API_KEY_TOKEN_PREFIX}_"):
        ctx = await _org_context_from_api_key(creds.credentials, session, request)
        if x_org_id and x_org_id != str(ctx.org.id):
            raise PermissionDeniedError("X-Org-Id does not match this API key's organization")
    else:
        user = await get_current_user(request, creds, session)
        if not x_org_id:
            raise ValidationFailedError("X-Org-Id header is required for org-scoped routes")
        try:
            org_id = uuid.UUID(x_org_id)
        except (ValueError, AttributeError) as exc:
            raise ValidationFailedError("X-Org-Id is not a valid UUID") from exc

        found = await orgs_repo.get_membership(session, org_id=org_id, user_id=user.id)
        if found is None:
            # Deliberately the same 403 whether the org does not exist or the user simply
            # is not a member — do not let callers probe for which orgs exist.
            raise PermissionDeniedError("You are not a member of this organization")

        org, membership, role = found
        if not org.is_active:
            raise PermissionDeniedError("This organization is disabled")

        set_org_context(session, org.id)
        ctx = OrgContext(org=org, membership=membership, role=role, session=session)

    # P25 IP allowlist: an org that sets ip_allowlist opts into a network restriction for
    # BOTH human and API-key auth. Fail closed when the client IP cannot be determined.
    if ctx.org.ip_allowlist:
        if not identity_svc.ip_in_allowlist(
            identity_svc.client_ip(request), ctx.org.ip_allowlist
        ):
            if user is None:
                event_email = ""
                detail = "api_key"
            else:
                event_email = user.email
                detail = None

            identity_svc.record_login_event(
                session,
                email=event_email,
                outcome="blocked_ip",
                user_id=ctx.actor_user_id,
                org_id=ctx.org.id,
                request=request,
                detail=detail,
            )
            await session.commit()
            raise PermissionDeniedError(
                "Access from this network is not allowed", code="ip_not_allowed"
            )

    # P25 2FA enforcement is human-path only: an API key has no TOTP enrollments and its
    # scope (not step-up auth) is the security boundary. The exempt list is deliberately
    # narrow (see _2FA_EXEMPT_PATH_PREFIXES): enough to enrol a factor and manage your own
    # sessions, and nothing that serves org data.
    if (
        user is not None
        and identity_svc.two_factor_required(ctx.org, user)
        and not any(
            request.url.path.startswith(prefix) for prefix in _2FA_EXEMPT_PATH_PREFIXES
        )
    ):
        raise PermissionDeniedError(
            "Two-factor authentication is required by this organization",
            code="two_factor_required",
        )

    # P17: give the carrier registry proxy (app/providers/registry_org.py) an org to
    # resolve for the lifetime of this request, so app.state.carriers picks DB-configured
    # provider credentials over the env fallback when this org has an active account.
    # Skipped entirely with no master key configured — a bare env deployment (today's
    # default) never pays for the lookup.
    settings: Settings = request.app.state.settings
    if credential_svc.master_key_present(settings) and not registry_org.is_primed(ctx.org.id):
        # Reuse the live global registry's adapter objects/HealthRegistry rather than
        # rebuilding both from settings - getattr because a test fixture may have
        # installed a raw CarrierRegistry (no .global_registry) in app.state.carriers.
        carriers = getattr(request.app.state, "carriers", None)
        global_registry = getattr(carriers, "global_registry", None)
        await registry_org.prime_org_registry(
            session, settings, ctx.org.id, global_registry=global_registry
        )
    org_token = registry_org.CURRENT_ORG_ID.set(ctx.org.id)
    try:
        yield ctx
    finally:
        registry_org.CURRENT_ORG_ID.reset(org_token)


def require_permission(permission: str):
    """Dependency factory. Owner's ``*`` short-circuits every check."""

    async def _check(ctx: Annotated[OrgContext, Depends(get_current_org)]) -> OrgContext:
        if not ctx.role.grants(permission):
            raise PermissionDeniedError(f"Requires permission: {permission}")
        return ctx

    return _check
