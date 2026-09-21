"""Tests for the Telnyx toll-free verification transport."""
from __future__ import annotations

import httpx
import pytest

from app.errors import FeatureUnavailableError, ValidationFailedError
from app.providers.telnyx.tollfree_verification import TelnyxTollfreeVerificationClient

BASE = "https://api.telnyx.test/v2"
REQ = "/messaging_tollfree/verification/requests"
LEAK = "carrier-detail-must-not-leak"


def _make(handler):
    transport = httpx.MockTransport(handler)
    return TelnyxTollfreeVerificationClient(
        api_key="KEY-test-secret", base_url=BASE,
        client=httpx.AsyncClient(transport=transport))


@pytest.mark.asyncio
async def test_post_and_get_transport():
    seen = []

    def handler(request):
        seen.append((request.method, str(request.url),
                     request.headers.get("Authorization")))
        return httpx.Response(200, json={"id": "tfv_abc", "status": "pending"})

    client = _make(handler)
    try:
        created = await client.create({"phone_numbers": ["+15550000001"]})
        fetched = await client.get("tfv 1/2")
    finally:
        await client.aclose()
    assert seen == [
        ("POST", f"{BASE}{REQ}", "Bearer KEY-test-secret"),
        ("GET", f"{BASE}{REQ}/tfv%201%2F2", "Bearer KEY-test-secret"),
    ]
    assert created["id"] == fetched["id"] == "tfv_abc"


@pytest.mark.parametrize("resp_kwargs,expected", [
    ({"status_code": 422, "json": {"detail": LEAK}}, ValidationFailedError),
    ({"status_code": 500, "json": {"detail": LEAK}}, FeatureUnavailableError),
    ({"status_code": 500, "text": "<html>error</html>"}, FeatureUnavailableError),
    ({"status_code": 200, "json": ["not", "a", "dict"]}, FeatureUnavailableError),
])
@pytest.mark.asyncio
async def test_error_responses_are_safe(resp_kwargs, expected):
    client = _make(lambda request: httpx.Response(**resp_kwargs))
    try:
        with pytest.raises(expected) as info:
            await client.create({"ein": "00-0000000"})
    finally:
        await client.aclose()
    assert LEAK not in str(info.value)


@pytest.mark.asyncio
async def test_post_timeout_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("read timed out")

    client = _make(handler)
    try:
        with pytest.raises(FeatureUnavailableError) as info:
            await client.create({"phone_numbers": ["+15550000002"]})
    finally:
        await client.aclose()
    assert len(calls) == 1
    assert "not retried" in str(info.value)
