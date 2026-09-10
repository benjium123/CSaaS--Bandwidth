"""Phase 21 / D28: voice adapters map rejections into the SAME CarrierError taxonomy the
messaging adapters feed into the breaker.

Tested without network by constructing each voice mixin over a stub httpx client.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers.health import opens_breaker
from app.providers.telnyx.voice import TelnyxVoiceMixin
from app.providers.twilio.voice import TwilioVoiceMixin


class StubResponse:
    def __init__(self, status_code: int, text: str = "", payload: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class StubClient:
    def __init__(self, response: StubResponse | None = None, exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc

    async def post(self, *args, **kwargs) -> StubResponse:
        if self.exc is not None:
            raise self.exc
        assert self.response is not None
        return self.response


class TelnyxHarness(TelnyxVoiceMixin):
    name = "telnyx"
    base_url = "https://api.telnyx.com/v2"
    api_key = "test-key"
    voice_connection_id = "test-connection"
    _public_key = ""

    def __init__(self, client: StubClient) -> None:
        self._client = client

    async def _get_client(self) -> StubClient:
        return self._client


class TwilioHarness(TwilioVoiceMixin):
    name = "twilio"
    base_url = "https://api.twilio.com/2010-04-01"
    _webhook_url = "https://example.com/voice"
    _auth = ("test-sid", "test-token")
    _auth_token = "test-token"

    def __init__(self, client: StubClient) -> None:
        self._client = client

    async def _get_client(self) -> StubClient:
        return self._client


def _make_harness(provider: str, client: StubClient):
    if provider == "telnyx":
        return TelnyxHarness(client)
    if provider == "twilio":
        return TwilioHarness(client)
    raise AssertionError(provider)


@pytest.mark.parametrize("provider", ["telnyx", "twilio"])
async def test_401_maps_to_auth_and_opens_breaker(provider: str) -> None:
    text = "carrier rejected credentials"
    response = StubResponse(401, text, {"code": "401", "message": text})
    harness = _make_harness(provider, StubClient(response=response))

    result = await harness.create_call(to="+19725550199", from_="+12145550100")

    assert result.status == "rejected"
    assert result.error.category == "auth"
    assert opens_breaker(result.error) is True
    assert result.error_detail == text


@pytest.mark.parametrize("provider", ["telnyx", "twilio"])
async def test_500_maps_to_carrier_transient_and_opens_breaker(provider: str) -> None:
    text = "carrier server exploded"
    response = StubResponse(500, text, {"code": "50000", "message": text})
    harness = _make_harness(provider, StubClient(response=response))

    result = await harness.create_call(to="+19725550199", from_="+12145550100")

    assert result.status == "rejected"
    assert result.error.category == "carrier_transient"
    assert opens_breaker(result.error) is True
    assert result.error_detail == text


@pytest.mark.parametrize("provider", ["telnyx", "twilio"])
async def test_400_invalid_request_code_does_not_open_breaker(provider: str) -> None:
    text = "Invalid request"
    if provider == "telnyx":
        payload: dict = {"errors": [{"code": "40001", "detail": text}]}
    else:
        payload = {"code": "21211", "message": text}
    response = StubResponse(400, text, payload)
    harness = _make_harness(provider, StubClient(response=response))

    result = await harness.create_call(to="+19725550199", from_="+12145550100")

    assert result.status == "rejected"
    assert result.error.category == "invalid_request"
    assert opens_breaker(result.error) is False
    assert result.error_detail == text


@pytest.mark.parametrize("provider", ["telnyx", "twilio"])
async def test_transport_error_maps_to_carrier_unreachable_and_opens_breaker(
    provider: str,
) -> None:
    text = "connection refused"
    harness = _make_harness(provider, StubClient(exc=httpx.HTTPError(text)))

    result = await harness.create_call(to="+19725550199", from_="+12145550100")

    assert result.status == "rejected"
    assert result.error.category == "carrier_unreachable"
    assert opens_breaker(result.error) is True
    assert result.error_detail == text
