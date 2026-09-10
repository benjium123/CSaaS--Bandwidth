from __future__ import annotations

import secrets
import uuid
from urllib.parse import urlencode

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import create_access_token, hash_password
from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.db.session import get_session
from app.errors import (
    FeatureUnavailableError,
    NotFoundError,
    PermissionDeniedError,
    UnauthenticatedError,
)
from app.models import Org, OrgMembership, Role, User
from app.rate_limit import enforce_rate_limit
from app.services import credentials as credentials_svc
from app.services import identity as identity_svc
from app.services import oidc

router = APIRouter(prefix="/api/v1/auth/sso", tags=["auth"])

SSO_SCOPE = "openid email profile"


def _usable_sso(config: object) -> bool:
    if not isinstance(config, dict):
        return False
    required = ("issuer", "client_id", "client_secret_encrypted", "domain")
    return all(
        isinstance(config.get(key), str) and config.get(key).strip()
        for key in required
    )


def _redirect_uri(settings: Settings) -> str:
    public_base_url = (settings.public_base_url or "").strip().rstrip("/")
    if not public_base_url:
        # A guessed redirect_uri is an open-redirect risk.
        raise FeatureUnavailableError(
            "Single sign-on needs PUBLIC_BASE_URL to be configured"
        )
    # D59: this is where the identity provider redirects the BROWSER, so it must be the
    # SPA route (matched client-side by SsoCallbackPage), not this API path - landing the
    # browser directly on /api/v1/... would show raw JSON and the console would never run.
    # SsoCallbackPage reads `code`/`state` from its own URL and calls the real API
    # endpoint below (GET /api/v1/auth/sso/callback) itself, via fetch, to do the exchange.
    return f"{public_base_url}/auth/sso/callback"


async def _org_by_slug(session: AsyncSession, slug: str) -> Org | None:
    # Org is not TenantScoped anyway, but this lookup must run before tenant context
    # exists, so request the unscoped execution option explicitly.
    stmt = (
        sa.select(Org)
        .where(Org.slug == slug)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _org_by_id(session: AsyncSession, org_id: uuid.UUID) -> Org | None:
    # Same justification as _org_by_slug: Org is the tenant, not TenantScoped.
    stmt = (
        sa.select(Org)
        .where(Org.id == org_id)
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _resolve_role(session: AsyncSession, org: Org) -> Role:
    """Resolve the role for a new SSO membership.

    Caller must already have ``set_org_context(session, org.id)`` in effect; Role is
    TenantScoped.
    """
    default_id = org.sso.get("default_role_id")
    if default_id:
        try:
            role_id = uuid.UUID(str(default_id))
            role = (
                await session.execute(sa.select(Role).where(Role.id == role_id))
            ).scalar_one_or_none()
            if role is not None:
                return role
        except ValueError:
            pass

    role = (
        await session.execute(
            sa.select(Role).where(Role.name == "agent", Role.is_system.is_(True))
        )
    ).scalar_one_or_none()
    if role is None:
        raise FeatureUnavailableError(
            "Single sign-on is not fully configured for this organization"
        )
    return role


@router.get("/{org_slug}/start")
async def sso_start(
    org_slug: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    # Keyed per org slug, not a single global "sso:start" bucket - one shared bucket
    # would let any caller exhaust the SSO login path for every tenant at once.
    await enforce_rate_limit(request, f"sso:start:{org_slug}")
    # NEVER fall back to a fresh Settings(): a second instance re-read from the
    # environment can carry a DIFFERENT (or empty) jwt_secret, and this route MINTS
    # access tokens. create_app always sets app.state.settings.
    settings: Settings = request.app.state.settings

    org = await _org_by_slug(session, org_slug)
    if org is None or not org.is_active or not _usable_sso(org.sso):
        # The internet must not be able to enumerate slugs or which slugs have SSO.
        raise NotFoundError("Single sign-on is not configured")

    issuer = org.sso["issuer"].strip()
    client_id = org.sso["client_id"].strip()
    discovery = await oidc.discover(issuer)

    nonce = oidc.new_nonce()
    state = await oidc.issue_state(settings, org_id=org.id, nonce=nonce)
    redirect_uri = _redirect_uri(settings)

    query = urlencode({
        "response_type": "code",
        "scope": SSO_SCOPE,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "nonce": nonce,
    })
    authorization_endpoint = discovery["authorization_endpoint"]
    url = (
        f"{authorization_endpoint}"
        f"{'&' if '?' in authorization_endpoint else '?'}{query}"
    )
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
async def sso_callback(
    request: Request,
    session: AsyncSession = Depends(get_session),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> dict:
    # Keyed by the caller's address for the same reason as /start.
    await enforce_rate_limit(
        request, f"sso:callback:{identity_svc.client_ip(request) or 'unknown'}"
    )
    # NEVER fall back to a fresh Settings(): a second instance re-read from the
    # environment can carry a DIFFERENT (or empty) jwt_secret, and this route MINTS
    # access tokens. create_app always sets app.state.settings.
    settings: Settings = request.app.state.settings

    if error is not None or not code or not state:
        raise UnauthenticatedError("Single sign-on failed")

    state_data = await oidc.consume_state(settings, state)
    if state_data is None:
        # Single-use state is the CSRF defence; a replay must look exactly like an
        # unknown or expired state.
        raise UnauthenticatedError("Single sign-on failed")

    try:
        org_id = uuid.UUID(str(state_data["org_id"]))
        nonce = str(state_data["nonce"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UnauthenticatedError("Single sign-on failed") from exc

    org = await _org_by_id(session, org_id)
    if org is None or not org.is_active or not _usable_sso(org.sso):
        raise UnauthenticatedError("Single sign-on failed")

    issuer = org.sso["issuer"].strip()
    client_id = org.sso["client_id"].strip()

    # If SSO config changed between /start and /callback, the id_token fails verification
    # below because we verify against the CURRENT issuer/client_id.
    discovery = await oidc.discover(issuer)

    client_secret = credentials_svc.decrypt(
        settings, org.sso["client_secret_encrypted"]
    )["client_secret"]

    tokens = await oidc.exchange_code(
        token_endpoint=discovery["token_endpoint"],
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=_redirect_uri(settings),
    )

    claims = await oidc.verify_id_token(
        tokens["id_token"],
        issuer=issuer,
        client_id=client_id,
        nonce=nonce,
        jwks_uri=discovery["jwks_uri"],
    )

    email = claims["email"]
    domain = str(org.sso["domain"]).strip().lower()
    if oidc.email_domain(email) != domain:
        identity_svc.record_login_event(
            session,
            email=email,
            outcome="bad_password",
            org_id=org.id,
            request=request,
            detail="sso_domain_mismatch",
        )
        await session.commit()
        raise PermissionDeniedError(
            "This account is not permitted to sign in to this organization",
            code="sso_domain_mismatch",
        )

    stmt = sa.select(User).where(sa.func.lower(User.email) == email).limit(1)
    user = (await session.execute(stmt)).scalar_one_or_none()

    if user is not None and not user.is_active:
        identity_svc.record_login_event(
            session,
            email=email,
            outcome="locked",
            user_id=user.id,
            org_id=org.id,
            request=request,
            detail="sso_user_inactive",
        )
        await session.commit()
        raise PermissionDeniedError("This account is disabled", code="account_locked")

    if user is None:
        # A new SSO user gets an unguessable random password so the row is never
        # password-loginable. An empty string would also not be a valid password hash.
        user = User(
            id=uuid.uuid4(),
            email=email,
            hashed_password=hash_password(secrets.token_urlsafe(32)),
            full_name=str(claims.get("name") or "").strip()[:255],
            is_active=True,
        )
        session.add(user)

    # OrgMembership and Role are TenantScoped; set the tenant context before touching
    # either of them.
    set_org_context(session, org.id)

    membership = (
        await session.execute(
            sa.select(OrgMembership).where(
                OrgMembership.org_id == org.id,
                OrgMembership.user_id == user.id,
            )
        )
    ).scalar_one_or_none()

    if membership is None:
        role = await _resolve_role(session, org)
        membership = OrgMembership(org_id=org.id, user_id=user.id, role_id=role.id)
        session.add(membership)

    identity_session = await identity_svc.create_session(
        session,
        user_id=user.id,
        org_id=org.id,
        request=request,
        expire_hours=settings.jwt_expire_hours,
    )
    await session.flush()

    access_token = create_access_token(
        user.id,
        settings.jwt_secret.get_secret_value(),
        expire_hours=settings.jwt_expire_hours,
        sid=identity_session.id,
    )

    identity_svc.record_login_event(
        session,
        email=email,
        outcome="sso",
        user_id=user.id,
        org_id=org.id,
        request=request,
    )

    await session.commit()

    # Return JSON, not a 302. A token in a query string lands in logs, Referer headers,
    # and browser history; the console is expected to complete the flow from this JSON.
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "org_id": str(org.id),
    }
