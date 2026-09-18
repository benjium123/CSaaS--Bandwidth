"""P42: SAML 2.0 sign-in routes. Protocol work lives in ``services/saml.py``; the sign-in
itself finishes through ``services/sso_provisioning.py`` exactly like OIDC."""

from __future__ import annotations

import hmac
import uuid
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.sso import _org_by_id, _org_by_slug
from app.config import Settings
from app.db.session import get_session
from app.errors import CsaasError, NotFoundError
from app.rate_limit import enforce_rate_limit
from app.services import identity as identity_svc
from app.services import oidc, saml, session_tokens, sso_provisioning

router = APIRouter(prefix="/api/v1/auth/saml", tags=["auth"])
logger = structlog.get_logger("saml")


def _console_url(settings: Settings, **params: str) -> str:
    base = (settings.public_web_url or settings.public_base_url or "").rstrip("/")
    return f"{base}/auth/sso/callback?{urlencode(params)}"


async def _saml_org(session: AsyncSession, slug: str):
    org = await _org_by_slug(session, slug)
    if org is None or not org.is_active or not saml.is_configured(org.sso):
        # Same answer for "no such workspace" and "no SAML": slugs must not be enumerable.
        raise NotFoundError("Single sign-on is not configured")
    return org


@router.get("/{org_slug}/metadata")
async def saml_metadata(
    org_slug: str, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    settings: Settings = request.app.state.settings
    org = await _org_by_slug(session, org_slug)
    if org is None or not org.is_active:
        raise NotFoundError("Single sign-on is not configured")
    # Served before the IdP is configured: the admin needs these values to set it up.
    return Response(saml.metadata_xml(settings, org), media_type="application/samlmetadata+xml")


@router.get("/{org_slug}/start")
async def saml_start(
    org_slug: str, request: Request, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    await enforce_rate_limit(request, f"saml:start:{org_slug}")
    settings: Settings = request.app.state.settings
    org = await _saml_org(session, org_slug)
    request_id = saml.new_request_id()
    relay_state = await oidc.issue_state(settings, org_id=org.id, nonce=request_id)
    response = RedirectResponse(
        saml.authn_request_url(settings, org, request_id, relay_state), status_code=302
    )
    _set_flow_cookie(response, settings, relay_state)
    return response


#: P43 (audit): ties the SAML response to the browser that STARTED the sign-in. RelayState
#: alone proves someone started a flow, not that this browser did, so an attacker could
#: drive a victim's browser into the attacker's workspace - where the victim would then
#: upload their ID document and selfie. SameSite=None is required because the ACS is a
#: cross-site POST from the IdP, and browsers only accept SameSite=None with Secure, so the
#: binding is enforced exactly where the cookie can exist: deployments on https.
FLOW_COOKIE = "csaas_saml_flow"


def _set_flow_cookie(response: Response, settings: Settings, relay_state: str) -> None:
    #: One cookie per browser, so starting a second sign-in in another tab replaces the
    #: first one and that tab's response is then refused. Usability edge, not a hole.
    if not session_tokens.cookie_secure(settings):
        # Plain-http dev: a SameSite=None cookie would be dropped by the browser, so the
        # binding is inactive. Say so once in production, where it means a deployment has
        # SESSION_COOKIE_SECURE=false (e.g. behind a TLS-terminating proxy) and has silently
        # turned this control off.
        if settings.is_production:
            logger.warning("saml_flow_binding_inactive", reason="session_cookie_secure=false")
        return
    response.set_cookie(
        FLOW_COOKIE,
        relay_state,
        max_age=600,
        httponly=True,
        secure=True,
        samesite="none",
        path="/api/v1/auth/saml",
    )


def _clear_flow_cookie(response: Response, settings: Settings) -> None:
    if session_tokens.cookie_secure(settings):
        response.delete_cookie(
            FLOW_COOKIE, path="/api/v1/auth/saml", secure=True, httponly=True, samesite="none"
        )


def _flow_cookie_matches(request: Request, settings: Settings, relay_state: str) -> bool:
    if not session_tokens.cookie_secure(settings):
        return True  # see _set_flow_cookie: no cookie is set on plain http
    sent = request.cookies.get(FLOW_COOKIE) or ""
    return bool(sent) and hmac.compare_digest(sent, relay_state)


async def _warn_if_certificate_expired(session: AsyncSession, org) -> None:  # noqa: ANN001
    """Open one alert per workspace while its IdP certificate is past its expiry."""
    import sqlalchemy as sa

    from app.models import SecurityAlert

    cert = (org.sso or {}).get("idp_x509_cert")
    if not cert or not saml.certificate_expired(str(cert)):
        return
    # SecurityAlert carries an org_id WITHOUT being TenantScoped, so no automatic filter
    # applies to it and the .where below is the only org boundary. (No allow_unscoped option
    # here: it would be a no-op on this model and would wrongly imply the row is guarded.)
    existing = (
        await session.execute(
            sa.select(SecurityAlert.id).where(
                SecurityAlert.kind == "sso_certificate_expired",
                SecurityAlert.org_id == org.id,
                SecurityAlert.status == "open",
            )
        )
    ).first()
    if existing is not None:
        return
    until = saml.certificate_valid_until(str(cert))
    session.add(
        SecurityAlert(
            id=uuid.uuid4(),
            kind="sso_certificate_expired",
            org_id=org.id,
            status="open",
            detail={
                "expired_on": until.date().isoformat() if until else None,
                "note": (
                    "Single sign-on still works, but this workspace's identity provider "
                    "certificate has expired. Ask them to paste the current one."
                ),
            },
        )
    )
    await session.commit()
    logger.warning("saml_certificate_expired", org=str(org.id))


@router.post("/{org_slug}/acs")
async def saml_acs(
    org_slug: str,
    request: Request,
    SAMLResponse: str = Form(...),  # noqa: N803 - SAML binding field names
    RelayState: str = Form(""),  # noqa: N803
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    await enforce_rate_limit(request, f"saml:acs:{identity_svc.client_ip(request) or 'unknown'}")
    settings: Settings = request.app.state.settings
    try:
        # Single-use RelayState ties this response to a sign-in someone started; the flow
        # cookie below ties it to THIS browser. Unsolicited (IdP-initiated) responses have
        # no RelayState at all and are refused here.
        state = await oidc.consume_state(settings, RelayState) if RelayState else None
        if state is None:
            raise saml.SamlError("unsolicited_or_expired")
        # The state proves A browser started this flow; the cookie proves it was THIS one.
        if not _flow_cookie_matches(request, settings, RelayState):
            raise saml.SamlError("flow_not_started_here")
        org = await _saml_org(session, org_slug)
        if uuid.UUID(str(state["org_id"])) != org.id:
            raise saml.SamlError("relay_state_other_org")
        # Re-read by id: the org must not have changed identity between start and ACS.
        org = await _org_by_id(session, org.id)
        if org is None or not saml.is_configured(org.sso):
            raise saml.SamlError("not_configured")
        identity = await saml.parse_response(
            settings, org, SAMLResponse, request_id=str(state["nonce"])
        )
        # P43 (audit): the pinned certificate is trusted as a KEY, not validated as a chain,
        # so an expired one keeps working rather than locking the workspace out mid-flight.
        # Sign-in still succeeds; the workspace is TOLD instead.
        await _warn_if_certificate_expired(session, org)
        redirect = RedirectResponse(
            _console_url(settings, saml="1", org_id=str(org.id)), status_code=303
        )
        _clear_flow_cookie(redirect, settings)
        await sso_provisioning.complete_sso_login(
            session,
            settings,
            request,
            redirect,
            org=org,
            email=identity.email,
            full_name=identity.name,
            groups=identity.groups,
            protocol="saml",
        )
        return redirect
    except CsaasError as exc:
        reason = getattr(exc, "reason", None)
        logger.warning("saml_login_refused", org=org_slug, code=exc.code, reason=reason)
        return RedirectResponse(_console_url(settings, error=exc.code), status_code=303)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("saml_login_refused", org=org_slug, reason=type(exc).__name__)
        return RedirectResponse(_console_url(settings, error="sso_failed"), status_code=303)
