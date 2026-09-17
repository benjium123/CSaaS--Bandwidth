"""P42: SAML 2.0 sign-in routes. Protocol work lives in ``services/saml.py``; the sign-in
itself finishes through ``services/sso_provisioning.py`` exactly like OIDC."""

from __future__ import annotations

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
from app.services import oidc, saml, sso_provisioning

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
    return RedirectResponse(
        saml.authn_request_url(settings, org, request_id, relay_state), status_code=302
    )


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
        # Single-use RelayState ties this response to a sign-in THIS browser started;
        # unsolicited (IdP-initiated) responses have none and are refused.
        state = await oidc.consume_state(settings, RelayState) if RelayState else None
        if state is None:
            raise saml.SamlError("unsolicited_or_expired")
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
        redirect = RedirectResponse(
            _console_url(settings, saml="1", org_id=str(org.id)), status_code=303
        )
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
