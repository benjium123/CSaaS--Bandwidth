"""Request-scoped dependencies: who is calling, for which org, with what permissions."""

from __future__ import annotations

import hmac
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
from app.errors import (
    FeatureUnavailableError,
    PermissionDeniedError,
    StepUpRequiredError,
    UnauthenticatedError,
    ValidationFailedError,
)
from app.models import ApiKey, Org, OrgMembership, PlatformOperator, Role, User
from app.models.rbac import WILDCARD as WILDCARD_PERMISSION
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
    settings: Settings = request.app.state.settings
    from app.services import session_tokens

    cookie = request.cookies.get(session_tokens.session_cookie_name(settings))
    # An explicit Authorization header wins over an ambient cookie: a cross-site page cannot
    # attach one, so this cannot be used to dodge the cookie path's CSRF check.
    if cookie and (creds is None or not creds.credentials):
        user_id = await _authenticate_cookie(request, session, settings, cookie)
        return await _finish_user(request, session, settings, user_id)

    if creds is None or not creds.credentials:
        raise UnauthenticatedError("Sign in to continue")
    if not settings.auth_bearer_compat:
        raise UnauthenticatedError("Sign in to continue")
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

    return await _finish_user(request, session, settings, user_id)


async def _authenticate_cookie(
    request: Request, session: AsyncSession, settings: Settings, cookie: str
) -> uuid.UUID:
    """P42: validate the session cookie; enforce absolute + idle timeouts and CSRF."""
    from app.models import Session as IdentitySession
    from app.services import session_tokens

    parsed = session_tokens.parse(cookie)
    if parsed is None:
        raise UnauthenticatedError("Sign in to continue")
    sid, secret = parsed
    row = await session.get(IdentitySession, sid)
    now = datetime.now(timezone.utc)
    if (
        row is None
        or row.revoked_at is not None
        or not session_tokens.secret_matches(row, secret)
        or _aware(row.expires_at) <= now
    ):
        raise UnauthenticatedError("Your session has ended. Sign in again.", code="session_expired")
    last_seen = _aware(row.last_seen_at) or _aware(row.created_at) or now
    if now - last_seen > timedelta(minutes=settings.session_idle_minutes):
        row.revoked_at = now
        await session.commit()
        raise UnauthenticatedError(
            "You were signed out after a period of inactivity.", code="session_expired"
        )
    if request.method.upper() not in session_tokens.SAFE_METHODS:
        sent = request.headers.get(session_tokens.CSRF_HEADER, "")
        if not sent or not hmac.compare_digest(sent, session_tokens.csrf_token(settings, sid)):
            raise PermissionDeniedError(
                "Security check failed. Reload the page.", code="csrf_failed"
            )
    request.state.session_id = sid
    request.state.session_last_seen = last_seen
    request.state.session_created = _aware(row.created_at)
    if now - last_seen >= timedelta(seconds=60):
        row.last_seen_at = now
        await session.commit()
    return row.user_id


async def _finish_user(
    request: Request, session: AsyncSession, settings: Settings, user_id: uuid.UUID
) -> User:
    user = await users_repo.get_by_id(session, user_id)
    if user is None:
        raise UnauthenticatedError("Invalid or expired token")
    if not user.is_active:
        raise UnauthenticatedError("Invalid or expired token")

    # P41: platform-wide mandatory second factor. Same narrow exempt list as the P25 org
    # policy - enough to enrol a factor and see/revoke your own sessions, nothing else.
    if (
        settings.require_2fa_all_users
        and not user.has_second_factor
        and not _path_exempt_from_2fa(request.url.path)
    ):
        raise PermissionDeniedError(
            "Set up an authenticator app or a passkey to continue",
            code="two_factor_required",
        )
    return user


def _enforce_org_session_policy(request: Request, org: Org) -> None:
    """P42: a workspace may demand shorter sessions than the platform default. Only cookie
    sessions carry the timestamps this needs (bearer-compat sessions keep platform rules)."""
    last_seen = getattr(request.state, "session_last_seen", None)
    created = getattr(request.state, "session_created", None)
    if last_seen is None or created is None:
        return
    now = datetime.now(timezone.utc)
    if org.session_idle_minutes and now - last_seen > timedelta(minutes=org.session_idle_minutes):
        raise UnauthenticatedError(
            "This workspace signs you out after a period of inactivity.", code="session_expired"
        )
    if org.session_max_hours and now - created > timedelta(hours=org.session_max_hours):
        raise UnauthenticatedError(
            "This workspace requires you to sign in again.", code="session_expired"
        )


def _path_exempt_from_2fa(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _2FA_EXEMPT_PATH_PREFIXES)


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

    # P42: a key restricted to certain networks is useless anywhere else.
    caller_ip = identity_svc.client_ip(request)
    if row.allowed_cidrs and not identity_svc.ip_in_allowlist(caller_ip, row.allowed_cidrs):
        await enforce_rate_limit(request, f"apikey:{prefix}")
        raise UnauthenticatedError("This API key cannot be used from this network")

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
    ip_changed = caller_ip is not None and row.last_used_ip != caller_ip
    if ip_changed:
        row.last_used_ip = caller_ip
    if last is None or (now - last) >= timedelta(hours=1) or ip_changed:
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
        _enforce_org_session_policy(request, org)

        set_org_context(session, org.id)
        ctx = OrgContext(org=org, membership=membership, role=role, session=session)
        from app.services import passkey_policy

        await passkey_policy.enforce(
            request,
            session,
            request.app.state.settings,
            user,
            org=org,
            privileged=passkey_policy.is_privileged_role(role),
        )
        set_org_context(session, org.id)

    # P25 IP allowlist: an org that sets ip_allowlist opts into a network restriction for
    # BOTH human and API-key auth. Fail closed when the client IP cannot be determined.
    if ctx.org.ip_allowlist:
        if not identity_svc.ip_in_allowlist(identity_svc.client_ip(request), ctx.org.ip_allowlist):
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
        and not any(request.url.path.startswith(prefix) for prefix in _2FA_EXEMPT_PATH_PREFIXES)
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

    async def _check(
        request: Request, ctx: Annotated[OrgContext, Depends(get_current_org)]
    ) -> OrgContext:
        if not ctx.role.grants(permission):
            raise PermissionDeniedError(f"Requires permission: {permission}")
        if permission in IDENTITY_GATED_PERMISSIONS:
            await _require_verified_privileged_member(request, ctx)
        return ctx

    return _check


#: P41: once a business is approved, a NON-owner may only use these powers after their own
#: ID + selfie check (operator decision: admin and billing roles are verified people).
IDENTITY_GATED_PERMISSIONS = frozenset(
    {
        "org:billing",
        "members:invite",
        "members:update",
        "members:remove",
        "roles:write",
        "numbers:manage",
    }
)


async def _require_verified_privileged_member(request: Request, ctx: OrgContext) -> None:
    settings: Settings = request.app.state.settings
    if not settings.kyc_enforced or ctx.membership is None:
        return
    if WILDCARD_PERMISSION in (ctx.role.permissions or []):
        return  # owners are the verified people on the application itself
    from app.models import KYC_TELEPHONY_STATUSES, KycPerson, KycProfile

    status = (
        await ctx.session.execute(
            sa.select(KycProfile.status).where(KycProfile.org_id == ctx.org.id)
        )
    ).scalar_one_or_none()
    if status not in KYC_TELEPHONY_STATUSES:
        return  # before approval the application itself is the gate
    verified = (
        await ctx.session.execute(
            sa.select(KycPerson.id).where(
                KycPerson.org_id == ctx.org.id,
                KycPerson.user_id == ctx.membership.user_id,
                KycPerson.status == "verified",
            )
        )
    ).first()
    if verified is None:
        raise PermissionDeniedError(
            "Verify your identity (ID + selfie) to use admin and billing features",
            code="identity_verification_required",
        )


# --------------------------------------------------------------------------------------
# P41 step-up: "prove it again" before a sensitive action
# --------------------------------------------------------------------------------------
STEP_UP_KINDS = ("recent_2fa", "recent_selfie")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def current_identity_session(request: Request, session: AsyncSession):
    """The live Session row behind this request, or None for a pre-P25 token / API key."""
    sid = getattr(request.state, "session_id", None)
    if sid is None:
        return None
    return await identity_svc.get_live_session(session, sid)


async def check_step_up(
    request: Request, session: AsyncSession, user: User, *, kind: str, action: str
) -> None:
    """Raise StepUpRequiredError unless ``user`` holds fresh proof of ``kind``.

    ``recent_2fa``: this session proved a second factor within STEP_UP_2FA_MINUTES.
    ``recent_selfie``: the signed-in person passed a Stripe Identity selfie check for
    ``action`` within STEP_UP_SELFIE_MINUTES (services/kyc_step_up.py).

    Callable directly for CONDITIONAL step-ups (e.g. only above a bulk-order threshold).
    """
    if kind not in STEP_UP_KINDS:
        raise ValueError(f"Unknown step-up kind: {kind}")
    settings: Settings = request.app.state.settings
    now = datetime.now(timezone.utc)
    _refuse_during_recovery_cooldown(user, now)
    if kind == "recent_2fa":
        row = await current_identity_session(request, session)
        proven = _aware(row.second_factor_at) if row is not None else None
        if proven is None or now - proven > timedelta(minutes=settings.step_up_2fa_minutes):
            raise StepUpRequiredError(kind=kind, action=action)
        return

    from app.services import kyc_step_up

    if not await kyc_step_up.has_fresh_selfie(session, settings, user, action=action, now=now):
        raise StepUpRequiredError(kind=kind, action=action)


def _refuse_during_recovery_cooldown(user: User, now: datetime) -> None:
    """P42: right after an ID-based account recovery, sensitive actions wait out a cool-down."""
    blocked = _aware(user.step_up_blocked_until)
    if blocked is not None and blocked > now:
        raise PermissionDeniedError(
            "This account was recently recovered. Sensitive changes unlock "
            f"{blocked.strftime('%Y-%m-%d %H:%M UTC')}.",
            code="recovery_cooldown",
        )


async def check_org_selfie_step_up(request: Request, ctx: OrgContext, *, action: str) -> None:
    """Selfie step-up for an org-scoped risky action. A no-op while KYC_ENFORCED is off.
    API keys are refused outright: no key can prove who is holding it."""
    settings: Settings = request.app.state.settings
    if ctx.membership is not None:
        user = await ctx.session.get(User, ctx.membership.user_id)
        if user is not None:
            _refuse_during_recovery_cooldown(user, datetime.now(timezone.utc))
            set_org_context(ctx.session, ctx.org.id)
    if not settings.kyc_enforced:
        return
    if ctx.membership is None:
        raise PermissionDeniedError(
            "This action needs a signed-in person, not an API key", code="step_up_required"
        )
    user = await ctx.session.get(User, ctx.membership.user_id)
    await check_step_up(request, ctx.session, user, kind="recent_selfie", action=action)
    set_org_context(ctx.session, ctx.org.id)


def require_step_up(kind: str, action: str):
    """Dependency form of check_step_up for human routes.

    API keys can never satisfy a step-up: the actions behind one are exactly the ones a
    leaked key must not be able to perform, so a key-authenticated call is refused.
    """
    if kind not in STEP_UP_KINDS:
        raise ValueError(f"Unknown step-up kind: {kind}")

    async def _check(
        request: Request,
        user: Annotated[User, Depends(get_current_user)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> User:
        await check_step_up(request, session, user, kind=kind, action=action)
        return user

    return _check


# --------------------------------------------------------------------------------------
# P41 platform operators
# --------------------------------------------------------------------------------------
@dataclass
class OperatorContext:
    user: User
    operator: PlatformOperator
    session: AsyncSession


async def _operator_check(
    request: Request, session: AsyncSession, user: User, role: str
) -> PlatformOperator:
    from app.services import operators as operators_svc

    operator = await operators_svc.get_active(session, user.id)
    if operator is None or not operators_svc.role_satisfies(operator.role, role):
        raise PermissionDeniedError("Platform operator access required")
    if not user.has_second_factor:
        raise PermissionDeniedError(
            "Operators must have an authenticator app or passkey",
            code="two_factor_required",
        )
    from app.services import passkey_policy

    await passkey_policy.enforce(
        request, session, request.app.state.settings, user, org=None, privileged=True
    )
    row = await current_identity_session(request, session)
    if row is None or row.second_factor_at is None:
        # Signed in with a password alone (possible only before the account had a
        # factor): the operator console always needs a session that proved one.
        raise StepUpRequiredError(kind="recent_2fa", action="operator_console")
    return operator


def require_operator(role: str = "reviewer"):
    """A NAMED operator: a signed-in user with an active platform_operators row, a second
    factor on the account (regardless of REQUIRE_2FA_ALL_USERS) and a session that proved
    it. The shared ops token is never accepted here - these routes read identity data and
    decide who may use the platform, so every action needs a person attached."""

    async def _check(
        request: Request,
        user: Annotated[User, Depends(get_current_user)],
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> OperatorContext:
        operator = await _operator_check(request, session, user, role)
        return OperatorContext(user=user, operator=operator, session=session)

    return _check


async def require_platform_operator(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
    x_platform_ops_token: Annotated[str | None, Header(alias="X-Platform-Ops-Token")] = None,
) -> None:
    """Legacy ops routes (billing knobs, registration status callbacks): the shared token
    for scripts, OR an admin operator's session. P41 merged the two identical copies that
    lived in routes/platform.py and routes/registration.py into this one."""
    configured = request.app.state.settings.platform_ops_token.get_secret_value().strip()
    if x_platform_ops_token:
        if not configured:
            raise FeatureUnavailableError(
                "Platform operator token is not configured; status callbacks are disabled"
            )
        # C6: constant-time compare - a naive != leaks timing information an attacker can
        # use to recover the token byte-by-byte.
        if not hmac.compare_digest(x_platform_ops_token, configured):
            raise PermissionDeniedError("Invalid platform operator token")
        return
    if (
        creds is not None
        and creds.credentials
        and not creds.credentials.startswith(f"{API_KEY_TOKEN_PREFIX}_")
    ):
        from app.services import operators as operators_svc

        user = await get_current_user(request, creds, session)
        if await operators_svc.get_active(session, user.id) is not None:
            await _operator_check(request, session, user, "admin")
            return
    if not configured:
        raise FeatureUnavailableError(
            "Platform operator token is not configured; status callbacks are disabled"
        )
    raise PermissionDeniedError("Invalid platform operator token")
