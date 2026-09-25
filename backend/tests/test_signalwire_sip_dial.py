"""The LaML answer we owe SignalWire for the SIP leg LiveKit opens.

The endpoint decides whether we pay for a phone call, so two things are worth defending: the
SIGNATURE, which must fail closed - an unsigned POST must never reach a <Dial> - and the SHAPE
of what we hand back, because a document SignalWire cannot parse is a dropped call.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Sequence
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.routes.webhooks import router
from app.providers.signalwire import sip_dial

BASE = "https://csaas.example.test"
#: SignalWire signs the public URL we advertise, not wherever a test app happens to mount the
#: router - the same distinction the live deployment makes.
SIGNING_URL = BASE + "/api/v1/webhooks/signalwire/sip-dial"
TOKEN = "swapi_test_token_not_a_real_one"
TO = "+14155550123"
FROM = "+14155559999"


def _sign(url: str, params: Sequence[tuple[str, str]], token: str = TOKEN) -> str:
    """Twilio's scheme, recomputed independently of the module under test: the URL first, then
    each param as name immediately followed by value, in name order."""
    payload = url + "".join(
        name + value for name, value in sorted(params, key=lambda pair: pair[0])
    )
    digest = hmac.new(token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _fields(to: str = TO, from_: str = FROM) -> list[tuple[str, str]]:
    return [("CallSid", "CA-123"), ("To", to), ("From", from_), ("Direction", "outbound-api")]


def _signature(params: Sequence[tuple[str, str]], url: str = SIGNING_URL) -> dict[str, str]:
    return {"X-Signalwire-Signature": _sign(url, params)}


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.state.settings = SimpleNamespace(
        public_base_url=BASE, signalwire_api_token=SecretStr(TOKEN)
    )
    app.include_router(router)
    return TestClient(app)


def _sip_dial_path() -> str:
    """The router may carry its own prefix, so the test asks it where the route landed instead
    of pinning a mount point it does not control."""
    paths = [
        route.path
        for route in router.routes
        if getattr(route, "path", "").endswith("/signalwire/sip-dial")
    ]
    assert len(paths) == 1, paths
    return paths[0]


# --- the document we hand back ------------------------------------------------------------


def test_build_laml_dials_the_number_with_our_caller_id() -> None:
    laml = sip_dial.build_laml(TO, FROM)
    assert laml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    # Without answerOnBridge SignalWire answers our SIP leg immediately and the caller hears
    # silence where ringback should be.
    assert 'answerOnBridge="true"' in laml
    assert f'callerId="{FROM}"' in laml
    assert f"<Number>{TO}</Number>" in laml
    assert laml.endswith("</Dial></Response>")


def test_build_laml_escapes_both_values() -> None:
    laml = sip_dial.build_laml("+1415555&<>", '+1415555"9')
    assert "<Number>+1415555&amp;&lt;&gt;</Number>" in laml
    assert 'callerId="+1415555&quot;9"' in laml


def test_signing_url_follows_the_public_base_url() -> None:
    assert sip_dial.signing_url("") == ""
    assert sip_dial.signing_url(BASE) == SIGNING_URL
    # A configured base url with a trailing slash must not double up in the signed string.
    assert sip_dial.signing_url(BASE + "/") == SIGNING_URL


def test_is_e164() -> None:
    assert sip_dial.is_e164(TO)
    assert not sip_dial.is_e164("notanumber")
    assert not sip_dial.is_e164("")
    assert not sip_dial.is_e164("+04155550123")


# --- the signature, which fails closed ----------------------------------------------------


def test_verify_accepts_a_signature_over_the_url_and_params() -> None:
    params = _fields()
    assert sip_dial.verify(_signature(params), urlencode(params).encode(), SIGNING_URL, TOKEN)


def test_verify_accepts_twilios_header_spelling() -> None:
    params = _fields()
    headers = {"X-Twilio-Signature": _sign(SIGNING_URL, params)}
    assert sip_dial.verify(headers, urlencode(params).encode(), SIGNING_URL, TOKEN)


def test_verify_rejects_a_wrong_signature() -> None:
    params = _fields()
    headers = {"X-Signalwire-Signature": _sign(SIGNING_URL, params, token="not-the-token")}
    assert not sip_dial.verify(headers, urlencode(params).encode(), SIGNING_URL, TOKEN)


def test_verify_rejects_a_missing_signature_header() -> None:
    assert not sip_dial.verify({}, urlencode(_fields()).encode(), SIGNING_URL, TOKEN)


def test_verify_fails_closed_without_a_signing_url() -> None:
    params = _fields()
    assert not sip_dial.verify(_signature(params), urlencode(params).encode(), "", TOKEN)


def test_verify_fails_closed_without_a_token() -> None:
    params = _fields()
    assert not sip_dial.verify(_signature(params), urlencode(params).encode(), SIGNING_URL, "")


# --- the route ----------------------------------------------------------------------------


def test_route_dials_a_signed_request(client: TestClient) -> None:
    params = _fields()
    response = client.post(_sip_dial_path(), data=dict(params), headers=_signature(params))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert f"<Number>{TO}</Number>" in response.text


def test_route_refuses_an_unsigned_request(client: TestClient) -> None:
    response = client.post(_sip_dial_path(), data=dict(_fields()))
    assert response.status_code == 403
    assert response.content == b""


def test_route_refuses_a_signed_request_to_a_non_number(client: TestClient) -> None:
    params = _fields(to="notanumber")
    response = client.post(_sip_dial_path(), data=dict(params), headers=_signature(params))
    assert response.status_code == 403
    assert response.content == b""
