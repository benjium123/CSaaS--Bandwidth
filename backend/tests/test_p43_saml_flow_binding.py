# ruff: noqa: E501
"""P43 (audit): a SAML response is only accepted by the browser that STARTED the sign-in.

RelayState alone proves that *someone* started a flow. Without a browser binding, an
attacker who starts a flow at their own workspace can post their own valid SAMLResponse
through a victim's browser and sign the victim into the ATTACKER's workspace - where the
victim then uploads their ID document and selfie. The flow cookie closes that.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.api.routes.saml import FLOW_COOKIE
from app.main import create_app
from tests.conftest import make_settings
from tests.test_p42_saml import (  # noqa: F401 - shared helpers
    BASE,
    _assertion,
    _error,
    _response,
    _saml_org,
    _sign,
    _start,
)


@pytest.fixture
async def secure_browser(engine):
    """Over https, so a SameSite=None; Secure cookie is actually carried."""
    settings = make_settings(
        public_base_url=BASE, public_web_url=BASE, sso_require_verified_domain=True
    )
    application = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="https://test"
    ) as client:
        yield client


async def _acs(client, org, saml_response: str, relay_state: str, cookies=None):
    return await client.post(
        f"/api/v1/auth/saml/{org.slug}/acs",
        data={"SAMLResponse": saml_response, "RelayState": relay_state},
        cookies=cookies,
        follow_redirects=False,
    )


async def test_sign_in_started_in_this_browser_is_accepted(secure_browser, session):
    org = await _saml_org(secure_browser, session)
    request_id, relay = await _start(secure_browser, org)
    assert secure_browser.cookies.get(FLOW_COOKIE) == relay, "the flow cookie should be set"
    signed = _sign(_assertion(org, request_id, f"a-{uuid.uuid4().hex[:6]}@saml-corp.example.com"))
    assert _error(await _acs(secure_browser, org, _response(org, request_id, signed), relay)) is None
    assert not secure_browser.cookies.get(FLOW_COOKIE), "the cookie is cleared after use"


async def test_response_posted_through_a_different_browser_is_refused(secure_browser, session):
    """The attack: the flow was started somewhere else, the victim's browser posts it."""
    org = await _saml_org(secure_browser, session)
    request_id, relay = await _start(secure_browser, org)
    signed = _sign(_assertion(org, request_id, f"v-{uuid.uuid4().hex[:6]}@saml-corp.example.com"))
    secure_browser.cookies.clear()  # a browser that never visited /start
    r = await _acs(secure_browser, org, _response(org, request_id, signed), relay)
    # Every SAML refusal reports the same code by design (the reason goes to the log only),
    # so the proof is that it was refused and no session was minted for the victim.
    assert _error(r) == "sso_failed"
    assert not r.cookies, r.cookies


async def test_a_forged_flow_cookie_does_not_help(secure_browser, session):
    org = await _saml_org(secure_browser, session)
    request_id, relay = await _start(secure_browser, org)
    signed = _sign(_assertion(org, request_id, f"f-{uuid.uuid4().hex[:6]}@saml-corp.example.com"))
    secure_browser.cookies.clear()
    r = await _acs(
        secure_browser,
        org,
        _response(org, request_id, signed),
        relay,
        cookies={FLOW_COOKIE: "not-the-relay-state"},
    )
    assert _error(r) == "sso_failed"
    assert not r.cookies, r.cookies
