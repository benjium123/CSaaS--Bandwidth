"""P42 SAML 2.0: signed assertions only, bound to our request, once, for a verified domain."""

from __future__ import annotations

import base64
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import sqlalchemy as sa
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import XMLSigner

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.main import create_app
from app.models import AuditLogEntry, OrgDomain, OrgMembership, User
from app.models import Session as IdentitySession
from app.services import oidc, saml, session_cache
from tests.conftest import create_org, make_settings, register_and_login

BASE = "https://app.example.com"
DOMAIN = "saml-corp.example.com"
IDP_ENTITY = "https://idp.saml-corp.example.com/metadata"
IDP_SSO = "https://idp.saml-corp.example.com/sso"
SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
EMAIL_FORMAT = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"


def _keypair(common_name: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key_pem, cert.public_bytes(serialization.Encoding.PEM)


IDP_KEY, IDP_CERT = _keypair("idp")
EVIL_KEY, EVIL_CERT = _keypair("evil")


@pytest.fixture(autouse=True)
def _reset():
    oidc.reset_caches()
    session_cache.reset_memory_cache()
    saml.reset_replay_cache()
    yield
    saml.reset_replay_cache()


@pytest.fixture
async def browser(engine):
    # These tests drive the ACS over http://test, so the SameSite=None; Secure flow cookie
    # could never ride along; session_cookie_secure=False turns the browser binding off for
    # them. The binding itself is covered by test_p43_saml_flow_binding.py over https.
    settings = make_settings(
        public_base_url=BASE,
        public_web_url=BASE,
        sso_require_verified_domain=True,
        session_cookie_secure=False,
    )
    application = create_app(settings)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _saml_org(client, session, *, verified: bool = True):
    token = await register_and_login(client, f"owner-{uuid.uuid4().hex[:8]}@{DOMAIN}")
    org_dict = await create_org(client, token, f"SAML {uuid.uuid4().hex[:6]}")
    from app.models import Org

    org = await session.get(Org, uuid.UUID(org_dict["id"]))
    org.sso = {
        "protocol": "saml",
        "domain": DOMAIN,
        "idp_entity_id": IDP_ENTITY,
        "idp_sso_url": IDP_SSO,
        "idp_x509_cert": IDP_CERT.decode(),
        "enforce": False,
    }
    set_org_context(session, org.id)
    session.add(
        OrgDomain(
            id=uuid.uuid4(),
            org_id=org.id,
            domain=DOMAIN,
            verify_token="t" * 48,
            verified_at=datetime.now(timezone.utc) if verified else None,
        )
    )
    await session.commit()
    await session.refresh(org)
    await session.commit()
    return org


async def _start(client, org) -> tuple[str, str]:
    r = await client.get(f"/api/v1/auth/saml/{org.slug}/start", follow_redirects=False)
    assert r.status_code == 302, r.text
    query = parse_qs(urlparse(r.headers["location"]).query)
    xml = zlib.decompress(base64.b64decode(query["SAMLRequest"][0]), -15)
    request_id = etree.fromstring(xml).get("ID")
    assert request_id
    return request_id, query["RelayState"][0]


def _ts(delta: timedelta = timedelta()) -> str:
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assertion(
    org,
    request_id: str,
    email: str,
    *,
    assertion_id: str | None = None,
    audience: str | None = None,
    not_on_or_after: timedelta = timedelta(minutes=5),
    issuer: str = IDP_ENTITY,
) -> etree._Element:
    aid = assertion_id or "_a" + uuid.uuid4().hex
    xml = f"""<saml:Assertion xmlns:saml="{SAML}" ID="{aid}" Version="2.0" IssueInstant="{_ts()}">
  <saml:Issuer>{issuer}</saml:Issuer>
  <saml:Subject>
    <saml:NameID Format="{EMAIL_FORMAT}">{email}</saml:NameID>
    <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
      <saml:SubjectConfirmationData InResponseTo="{request_id}"
        NotOnOrAfter="{_ts(not_on_or_after)}" Recipient="{BASE}/api/v1/auth/saml/{org.slug}/acs"/>
    </saml:SubjectConfirmation>
  </saml:Subject>
  <saml:Conditions NotBefore="{_ts(timedelta(minutes=-1))}" NotOnOrAfter="{_ts(not_on_or_after)}">
    <saml:AudienceRestriction>
      <saml:Audience>{audience or f"{BASE}/api/v1/auth/saml/{org.slug}/metadata"}</saml:Audience>
    </saml:AudienceRestriction>
  </saml:Conditions>
  <saml:AttributeStatement>
    <saml:Attribute Name="email"><saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute>
    <saml:Attribute Name="displayName">
      <saml:AttributeValue>Sam L</saml:AttributeValue>
    </saml:Attribute>
  </saml:AttributeStatement>
</saml:Assertion>"""
    return etree.fromstring(xml.encode())


def _sign(element: etree._Element, key: bytes = IDP_KEY, cert: bytes = IDP_CERT):
    return XMLSigner(
        signature_algorithm="rsa-sha256",
        digest_algorithm="sha256",
        c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    ).sign(element, key=key, cert=cert.decode(), reference_uri=element.get("ID"))


def _response(org, request_id: str, *assertions: etree._Element, sign_response=False) -> str:
    root = etree.fromstring(
        f"""<samlp:Response xmlns:samlp="{SAMLP}" xmlns:saml="{SAML}" ID="_r{uuid.uuid4().hex}"
  Version="2.0" IssueInstant="{_ts()}" InResponseTo="{request_id}"
  Destination="{BASE}/api/v1/auth/saml/{org.slug}/acs">
  <saml:Issuer>{IDP_ENTITY}</saml:Issuer>
  <samlp:Status><samlp:StatusCode Value="{saml.STATUS_SUCCESS}"/></samlp:Status>
</samlp:Response>""".encode()
    )
    for assertion in assertions:
        root.append(assertion)
    if sign_response:
        root = _sign(root)
    return base64.b64encode(etree.tostring(root)).decode()


async def _acs(client, org, saml_response: str, relay_state: str) -> httpx.Response:
    return await client.post(
        f"/api/v1/auth/saml/{org.slug}/acs",
        data={"SAMLResponse": saml_response, "RelayState": relay_state},
        follow_redirects=False,
    )


def _error(r: httpx.Response) -> str | None:
    assert r.status_code == 303, r.text
    return parse_qs(urlparse(r.headers["location"]).query).get("error", [None])[0]


async def test_metadata_describes_the_service_provider(browser, session):
    org = await _saml_org(browser, session)
    r = await browser.get(f"/api/v1/auth/saml/{org.slug}/metadata")
    assert r.status_code == 200
    assert f'entityID="{BASE}/api/v1/auth/saml/{org.slug}/metadata"' in r.text
    assert f"{BASE}/api/v1/auth/saml/{org.slug}/acs" in r.text


async def test_generic_sso_link_forwards_to_saml(browser, session):
    org = await _saml_org(browser, session)
    r = await browser.get(f"/api/v1/auth/sso/{org.slug}/start", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == f"/api/v1/auth/saml/{org.slug}/start"


async def test_signed_assertion_signs_in_and_provisions(browser, session):
    org = await _saml_org(browser, session)
    email = f"new-{uuid.uuid4().hex[:6]}@{DOMAIN}"
    request_id, relay = await _start(browser, org)
    signed = _sign(_assertion(org, request_id, email))
    r = await _acs(browser, org, _response(org, request_id, signed), relay)
    assert _error(r) is None
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["saml"] == ["1"] and query["org_id"] == [str(org.id)]
    cookies = [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
    assert any("csaas_session=" in c and "HttpOnly" in c for c in cookies)

    user = (
        await session.execute(
            sa.select(User)
            .where(User.email == email)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    assert user.full_name == "Sam L"
    membership = (
        await session.execute(
            sa.select(OrgMembership)
            .where(OrgMembership.user_id == user.id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    assert membership.org_id == org.id
    live = (
        await session.execute(sa.select(IdentitySession).where(IdentitySession.user_id == user.id))
    ).scalar_one()
    assert live.auth_method == "sso"
    actions = (
        (
            await session.execute(
                sa.select(AuditLogEntry.action)
                .where(AuditLogEntry.org_id == org.id)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    assert "sso.user_provisioned" in actions


async def test_signed_response_with_unsigned_assertion_is_accepted(browser, session):
    org = await _saml_org(browser, session)
    request_id, relay = await _start(browser, org)
    assertion = _assertion(org, request_id, f"r-{uuid.uuid4().hex[:6]}@{DOMAIN}")
    r = await _acs(browser, org, _response(org, request_id, assertion, sign_response=True), relay)
    assert _error(r) is None


async def test_unsigned_assertion_is_refused(browser, session):
    org = await _saml_org(browser, session)
    request_id, relay = await _start(browser, org)
    assertion = _assertion(org, request_id, f"u-{uuid.uuid4().hex[:6]}@{DOMAIN}")
    r = await _acs(browser, org, _response(org, request_id, assertion), relay)
    assert _error(r) == "sso_failed"


async def test_assertion_signed_by_another_key_is_refused(browser, session):
    org = await _saml_org(browser, session)
    request_id, relay = await _start(browser, org)
    signed = _sign(
        _assertion(org, request_id, f"e-{uuid.uuid4().hex[:6]}@{DOMAIN}"), EVIL_KEY, EVIL_CERT
    )
    r = await _acs(browser, org, _response(org, request_id, signed), relay)
    assert _error(r) == "sso_failed"


async def test_wrapped_second_assertion_is_refused(browser, session):
    """Signature wrapping: a genuinely signed assertion plus an attacker's unsigned one."""
    org = await _saml_org(browser, session)
    request_id, relay = await _start(browser, org)
    signed = _sign(_assertion(org, request_id, f"real-{uuid.uuid4().hex[:6]}@{DOMAIN}"))
    forged = _assertion(org, request_id, f"owner-forged@{DOMAIN}")
    r = await _acs(browser, org, _response(org, request_id, forged, signed), relay)
    assert _error(r) == "sso_failed"


async def test_tampered_signed_assertion_is_refused(browser, session):
    org = await _saml_org(browser, session)
    request_id, relay = await _start(browser, org)
    signed = _sign(_assertion(org, request_id, f"t-{uuid.uuid4().hex[:6]}@{DOMAIN}"))
    name_id = signed.find(f"{{{SAML}}}Subject/{{{SAML}}}NameID")
    name_id.text = f"victim@{DOMAIN}"
    for value in signed.iter(f"{{{SAML}}}AttributeValue"):
        if "@" in (value.text or ""):
            value.text = f"victim@{DOMAIN}"
    r = await _acs(browser, org, _response(org, request_id, signed), relay)
    assert _error(r) == "sso_failed"


async def test_replayed_assertion_is_refused(browser, session):
    org = await _saml_org(browser, session)
    email = f"rp-{uuid.uuid4().hex[:6]}@{DOMAIN}"
    request_id, relay = await _start(browser, org)
    signed = _sign(_assertion(org, request_id, email, assertion_id="_replay1"))
    assert _error(await _acs(browser, org, _response(org, request_id, signed), relay)) is None

    # Same assertion ID answering a fresh request.
    request_id2, relay2 = await _start(browser, org)
    again = _sign(_assertion(org, request_id2, email, assertion_id="_replay1"))
    assert (
        _error(await _acs(browser, org, _response(org, request_id2, again), relay2)) == "sso_failed"
    )


async def test_relay_state_is_single_use(browser, session):
    org = await _saml_org(browser, session)
    email = f"rs-{uuid.uuid4().hex[:6]}@{DOMAIN}"
    request_id, relay = await _start(browser, org)
    body = _response(org, request_id, _sign(_assertion(org, request_id, email)))
    assert _error(await _acs(browser, org, body, relay)) is None
    assert _error(await _acs(browser, org, body, relay)) == "sso_failed"


async def test_expired_wrong_audience_wrong_request_and_unsolicited_are_refused(browser, session):
    org = await _saml_org(browser, session)
    email = f"x-{uuid.uuid4().hex[:6]}@{DOMAIN}"

    request_id, relay = await _start(browser, org)
    expired = _sign(_assertion(org, request_id, email, not_on_or_after=timedelta(minutes=-10)))
    assert (
        _error(await _acs(browser, org, _response(org, request_id, expired), relay)) == "sso_failed"
    )

    request_id, relay = await _start(browser, org)
    audience = _sign(_assertion(org, request_id, email, audience="https://other.example.com"))
    assert (
        _error(await _acs(browser, org, _response(org, request_id, audience), relay))
        == "sso_failed"
    )

    request_id, relay = await _start(browser, org)
    other = _sign(_assertion(org, "_someone_elses_request", email))
    assert (
        _error(await _acs(browser, org, _response(org, request_id, other), relay)) == "sso_failed"
    )

    request_id, relay = await _start(browser, org)
    issuer = _sign(_assertion(org, request_id, email, issuer="https://evil.example.com"))
    assert (
        _error(await _acs(browser, org, _response(org, request_id, issuer), relay)) == "sso_failed"
    )

    unsolicited = _sign(_assertion(org, "", email))
    assert _error(await _acs(browser, org, _response(org, "", unsolicited), "")) == "sso_failed"


async def test_unverified_domain_cannot_sign_anyone_in(browser, session):
    org = await _saml_org(browser, session, verified=False)
    request_id, relay = await _start(browser, org)
    signed = _sign(_assertion(org, request_id, f"n-{uuid.uuid4().hex[:6]}@{DOMAIN}"))
    r = await _acs(browser, org, _response(org, request_id, signed), relay)
    assert _error(r) == "sso_domain_unverified"


async def test_email_outside_the_sso_domain_is_refused(browser, session):
    org = await _saml_org(browser, session)
    request_id, relay = await _start(browser, org)
    signed = _sign(_assertion(org, request_id, "someone@elsewhere.example.org"))
    r = await _acs(browser, org, _response(org, request_id, signed), relay)
    assert _error(r) == "sso_domain_mismatch"
