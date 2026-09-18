"""P42: SAML 2.0 service provider (SP-initiated, HTTP-Redirect out, HTTP-POST back).

Only what enterprise identity providers (Okta, Entra ID, Google Workspace, OneLogin,
JumpCloud) actually need, and nothing that widens the attack surface:

- SP-initiated only. An unsolicited (IdP-initiated) response has no request to answer and
  is refused - that is the classic login-CSRF / replay path.
- The assertion must be signed by the certificate the workspace uploaded, either directly
  or as the single assertion inside a signed Response. Everything is read from the element
  signxml returns as verified, never from the raw document (XML signature wrapping).
- Encrypted assertions, SHA-1 signatures, DTDs and external entities are refused.
- Issuer, Audience, Recipient, time windows (with SAML_CLOCK_SKEW_SECONDS) and InResponseTo
  are all checked; each assertion ID is accepted once (Redis in production).
"""

from __future__ import annotations

import base64
import secrets
import time
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock
from urllib.parse import urlencode
from xml.sax.saxutils import escape, quoteattr

import structlog
from cryptography import x509
from lxml import etree
from signxml import XMLVerifier
from signxml.verifier import SignatureConfiguration

from app.config import Settings
from app.errors import UnauthenticatedError, ValidationFailedError
from app.models import Org
from app.services import oidc

logger = structlog.get_logger("saml")

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}
_P = "{" + NS["samlp"] + "}"
_A = "{" + NS["saml"] + "}"
STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
MAX_RESPONSE_BYTES = 256_000

EMAIL_ATTRIBUTES = (
    "email",
    "mail",
    "emailaddress",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    "urn:oid:0.9.2342.19200300.100.1.3",
)
NAME_ATTRIBUTES = (
    "name",
    "displayname",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    "http://schemas.microsoft.com/identity/claims/displayname",
    "urn:oid:2.16.840.1.113730.3.1.241",
)
GROUP_ATTRIBUTES = (
    "groups",
    "memberof",
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
)


class SamlError(UnauthenticatedError):
    """Every SAML failure looks the same to the browser; the reason goes to the log."""

    def __init__(self, reason: str) -> None:
        super().__init__("Single sign-on failed", code="sso_failed")
        self.reason = reason


@dataclass
class SamlIdentity:
    email: str
    name: str
    groups: list[str] = field(default_factory=list)
    assertion_id: str = ""
    expires_at: float = 0.0


# --- configuration --------------------------------------------------------------------


def _base_url(settings: Settings) -> str:
    base = (settings.public_base_url or "").strip().rstrip("/")
    if not base:
        from app.errors import FeatureUnavailableError

        raise FeatureUnavailableError("Single sign-on needs PUBLIC_BASE_URL to be configured")
    return base


def sp_entity_id(settings: Settings, org: Org) -> str:
    return f"{_base_url(settings)}/api/v1/auth/saml/{org.slug}/metadata"


def acs_url(settings: Settings, org: Org) -> str:
    return f"{_base_url(settings)}/api/v1/auth/saml/{org.slug}/acs"


def is_configured(config: object) -> bool:
    if not isinstance(config, dict) or config.get("protocol") != "saml":
        return False
    return all(
        isinstance(config.get(key), str) and config[key].strip()
        for key in ("idp_entity_id", "idp_sso_url", "idp_x509_cert", "domain")
    )


def load_certificate(value: str) -> x509.Certificate:
    """Accept a PEM certificate or the bare base64 body IdPs often show."""
    text = (value or "").strip()
    if "BEGIN CERTIFICATE" not in text:
        body = "".join(text.split())
        lines = [body[i : i + 64] for i in range(0, len(body), 64)]
        text = "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----"
    try:
        return x509.load_pem_x509_certificate(text.encode())
    except ValueError as exc:
        raise ValidationFailedError("The identity provider certificate is not valid") from exc


def certificate_pem(value: str) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding

    return load_certificate(value).public_bytes(Encoding.PEM).decode()


def certificate_valid_until(value: str) -> datetime | None:
    """When the pinned IdP certificate stops being valid, or None if it can't be read."""
    try:
        cert = load_certificate(value)
    except ValidationFailedError:
        return None
    not_after = cert.not_valid_after_utc
    return not_after if not_after.tzinfo else not_after.replace(tzinfo=timezone.utc)


def certificate_expired(value: str, *, now: datetime | None = None) -> bool:
    until = certificate_valid_until(value)
    return until is not None and until <= (now or datetime.now(timezone.utc))


def check_certificate_usable(value: str) -> None:
    """Refuse a certificate that is already expired (or not yet valid) AT SAVE TIME.

    Deliberately not enforced at sign-in: signxml is given this certificate as a pinned
    key, not a chain to validate, and refusing an expired one mid-flight would lock a whole
    workspace out of SSO exactly when nobody can paste a new one - with enforce on there may
    be no password way back in. So the admin is stopped here, where they can fix it, and a
    certificate that expires later is surfaced as an alert instead (routes/saml.py).
    """
    cert = load_certificate(value)
    now = datetime.now(timezone.utc)
    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    if not_before.tzinfo is None:
        not_before = not_before.replace(tzinfo=timezone.utc)
    if not_after.tzinfo is None:
        not_after = not_after.replace(tzinfo=timezone.utc)
    if not_after <= now:
        raise ValidationFailedError(
            f"That identity provider certificate expired on {not_after.date().isoformat()}. "
            "Paste the current one from your identity provider."
        )
    if not_before > now:
        raise ValidationFailedError(
            f"That identity provider certificate is not valid until "
            f"{not_before.date().isoformat()}."
        )


def metadata_xml(settings: Settings, org: Org) -> str:
    entity = quoteattr(sp_entity_id(settings, org))
    acs = quoteattr(acs_url(settings, org))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID={entity}>'
        '<md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true" '
        'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        "<md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>"
        '<md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'Location={acs} index="0" isDefault="true"/>'
        "</md:SPSSODescriptor></md:EntityDescriptor>"
    )


# --- AuthnRequest -----------------------------------------------------------------------


def new_request_id() -> str:
    # XML IDs must not start with a digit.
    return "_" + secrets.token_hex(20)


def authn_request_url(settings: Settings, org: Org, request_id: str, relay_state: str) -> str:
    config = org.sso or {}
    issue_instant = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    destination = config["idp_sso_url"].strip()
    xml = (
        f'<samlp:AuthnRequest xmlns:samlp="{NS["samlp"]}" xmlns:saml="{NS["saml"]}" '
        f'ID="{request_id}" Version="2.0" IssueInstant="{issue_instant}" '
        f"Destination={quoteattr(destination)} "
        f"AssertionConsumerServiceURL={quoteattr(acs_url(settings, org))} "
        'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">'
        f"<saml:Issuer>{escape(sp_entity_id(settings, org))}</saml:Issuer>"
        '<samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress" '
        'AllowCreate="true"/>'
        "</samlp:AuthnRequest>"
    )
    compressor = zlib.compressobj(wbits=-15)
    deflated = compressor.compress(xml.encode()) + compressor.flush()
    query = urlencode(
        {"SAMLRequest": base64.b64encode(deflated).decode(), "RelayState": relay_state}
    )
    return f"{destination}{'&' if '?' in destination else '?'}{query}"


# --- replay store -------------------------------------------------------------------------

_seen: OrderedDict[str, float] = OrderedDict()
_seen_lock = Lock()
_SEEN_MAX = 50_000


def reset_replay_cache() -> None:
    with _seen_lock:
        _seen.clear()


async def _first_use(settings: Settings, assertion_id: str, expires_at: float) -> bool:
    ttl = max(60, int(expires_at - time.time()) + settings.saml_clock_skew_seconds)
    key = f"saml:assertion:{assertion_id}"
    client = oidc._state_redis_client(settings)
    if client is not None:
        try:
            return bool(await client.set(key, "1", ex=ttl, nx=True))
        except Exception as exc:  # fail closed: without the store a replay can't be ruled out
            logger.warning("saml_replay_store_unavailable", error=type(exc).__name__)
            return False
    now = time.time()
    with _seen_lock:
        while _seen:
            oldest_key, oldest_exp = next(iter(_seen.items()))
            if oldest_exp <= now or len(_seen) > _SEEN_MAX:
                _seen.pop(oldest_key)
            else:
                break
        if key in _seen:
            return False
        _seen[key] = now + ttl
        return True


# --- response verification ----------------------------------------------------------------


def _parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        dtd_validation=False,
        load_dtd=False,
        huge_tree=False,
    )


def _parse_time(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SamlError("bad_timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _verify(element: etree._Element, cert: x509.Certificate) -> etree._Element:
    """Verify the ds:Signature that is a direct child of ``element``; return what it signed."""
    standalone = etree.fromstring(etree.tostring(element), parser=_parser())
    result = XMLVerifier().verify(
        standalone,
        x509_cert=cert,
        expect_config=SignatureConfiguration(location="./", expect_references=1),
    )
    signed = result.signed_xml
    if signed is None or signed.tag != element.tag:
        raise SamlError("signature_covers_wrong_element")
    return signed


def _trusted_assertion(root: etree._Element, cert: x509.Certificate) -> etree._Element:
    if root.tag != f"{_P}Response":
        raise SamlError("not_a_response")
    if root.findall(f".//{_A}EncryptedAssertion"):
        raise SamlError("encrypted_assertion_unsupported")
    assertions = root.findall(f".//{_A}Assertion")
    direct = root.findall(f"{_A}Assertion")
    if len(assertions) != 1 or len(direct) != 1:
        raise SamlError("expected_one_assertion")
    assertion = direct[0]

    if assertion.find("ds:Signature", NS) is not None:
        try:
            return _verify(assertion, cert)
        except SamlError:
            raise
        except Exception as exc:
            raise SamlError(f"assertion_signature_invalid:{type(exc).__name__}") from exc

    if root.find("ds:Signature", NS) is None:
        raise SamlError("unsigned")
    try:
        signed_response = _verify(root, cert)
    except SamlError:
        raise
    except Exception as exc:
        raise SamlError(f"response_signature_invalid:{type(exc).__name__}") from exc
    inner = signed_response.findall(f"{_A}Assertion")
    if len(inner) != 1 or signed_response.findall(f".//{_A}EncryptedAssertion"):
        raise SamlError("expected_one_assertion")
    status = signed_response.find("samlp:Status/samlp:StatusCode", NS)
    if status is None or status.get("Value") != STATUS_SUCCESS:
        raise SamlError("status_not_success")
    return inner[0]


def _attributes(assertion: etree._Element) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for attribute in assertion.findall("saml:AttributeStatement/saml:Attribute", NS):
        names = {
            (attribute.get("Name") or "").strip().lower(),
            (attribute.get("FriendlyName") or "").strip().lower(),
        }
        values = [
            (value.text or "").strip()
            for value in attribute.findall("saml:AttributeValue", NS)
            if (value.text or "").strip()
        ]
        for name in names - {""}:
            out.setdefault(name, []).extend(values)
    return out


def _first(attributes: dict[str, list[str]], names: tuple[str, ...]) -> str:
    for name in names:
        values = attributes.get(name.lower())
        if values:
            return values[0]
    return ""


async def parse_response(
    settings: Settings, org: Org, saml_response: str, *, request_id: str
) -> SamlIdentity:
    config = org.sso or {}
    if not is_configured(config):
        raise SamlError("not_configured")
    if len(saml_response) > MAX_RESPONSE_BYTES:
        raise SamlError("too_large")
    try:
        raw = base64.b64decode(saml_response, validate=False)
    except ValueError as exc:
        raise SamlError("bad_base64") from exc
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise SamlError("dtd_refused")
    try:
        root = etree.fromstring(raw, parser=_parser())
    except etree.XMLSyntaxError as exc:
        raise SamlError("bad_xml") from exc

    # The outer status is checked even when only the assertion is signed.
    outer_status = root.find("samlp:Status/samlp:StatusCode", NS)
    if outer_status is None or outer_status.get("Value") != STATUS_SUCCESS:
        raise SamlError("status_not_success")

    cert = load_certificate(config["idp_x509_cert"])
    assertion = _trusted_assertion(root, cert)

    now = time.time()
    skew = settings.saml_clock_skew_seconds
    expected_issuer = config["idp_entity_id"].strip()
    expected_audience = sp_entity_id(settings, org)
    expected_recipient = acs_url(settings, org)

    issuer = (assertion.findtext("saml:Issuer", default="", namespaces=NS) or "").strip()
    if issuer != expected_issuer:
        raise SamlError("wrong_issuer")

    conditions = assertion.find("saml:Conditions", NS)
    if conditions is None:
        raise SamlError("no_conditions")
    not_before = _parse_time(conditions.get("NotBefore"))
    not_on_or_after = _parse_time(conditions.get("NotOnOrAfter"))
    if not_before is not None and now + skew < not_before:
        raise SamlError("not_yet_valid")
    if not_on_or_after is None or now - skew >= not_on_or_after:
        raise SamlError("expired")
    audiences = [
        (a.text or "").strip()
        for a in conditions.findall("saml:AudienceRestriction/saml:Audience", NS)
    ]
    if expected_audience not in audiences:
        raise SamlError("wrong_audience")

    confirmed = False
    for confirmation in assertion.findall("saml:Subject/saml:SubjectConfirmation", NS):
        if confirmation.get("Method") != BEARER:
            continue
        data = confirmation.find("saml:SubjectConfirmationData", NS)
        if data is None:
            continue
        if data.get("Recipient") != expected_recipient:
            continue
        if data.get("InResponseTo") != request_id:
            continue
        expiry = _parse_time(data.get("NotOnOrAfter"))
        if expiry is None or now - skew >= expiry:
            continue
        if data.get("NotBefore") is not None:
            continue  # a bearer confirmation must not carry NotBefore (SAML profiles 4.1.4.2)
        not_on_or_after = min(not_on_or_after, expiry)
        confirmed = True
        break
    if not confirmed:
        raise SamlError("subject_not_confirmed")

    attributes = _attributes(assertion)
    email = _first(attributes, EMAIL_ATTRIBUTES)
    if not email:
        name_id = assertion.find("saml:Subject/saml:NameID", NS)
        if name_id is not None and "@" in (name_id.text or ""):
            email = (name_id.text or "").strip()
    if "@" not in email:
        raise SamlError("no_email")
    groups: list[str] = []
    for name in GROUP_ATTRIBUTES:
        groups.extend(attributes.get(name, []))

    assertion_id = assertion.get("ID") or ""
    if not assertion_id:
        raise SamlError("no_assertion_id")
    if not await _first_use(settings, assertion_id, not_on_or_after):
        raise SamlError("replayed")

    return SamlIdentity(
        email=email.lower(),
        name=_first(attributes, NAME_ATTRIBUTES),
        groups=groups,
        assertion_id=assertion_id,
        expires_at=not_on_or_after,
    )
