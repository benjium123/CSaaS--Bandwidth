"""Focused tests for the Telnyx 10DLC registration transport client.

Every call goes through ``httpx.MockTransport`` - no network, no live Telnyx account,
no credentials. These tests pin the documented paths, the Bearer auth shape, the
flat-JSON response parsing and the error mapping (4xx/5xx/malformed/timeout).
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.errors import FeatureUnavailableError, ValidationFailedError
from app.providers.telnyx.registration import TelnyxRegistrationClient


def make_client(handler) -> tuple[TelnyxRegistrationClient, httpx.AsyncClient]:
    """Build a client over a MockTransport so no real request ever leaves the process."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TelnyxRegistrationClient(api_key="test-telnyx-key", client=http)
    return client, http


# ==================================================================================
# Happy path: the documented endpoints, verbatim payloads, flat-JSON responses
# ==================================================================================
async def test_create_brand_posts_documented_path_and_bearer_auth():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        # A real POST /10dlc/brand reply is FLAT - brandId sits at the top level.
        return httpx.Response(200, json={"brandId": "brand-123", "status": "PENDING"})

    client, http = make_client(handler)
    async with http:
        result = await client.create_brand(
            {"entityType": "PRIVATE_PROFIT", "displayName": "Acme LLC"}
        )

    assert seen["method"] == "POST"
    assert seen["path"] == "/v2/10dlc/brand"
    assert seen["auth"] == "Bearer test-telnyx-key"
    assert seen["body"] == {"entityType": "PRIVATE_PROFIT", "displayName": "Acme LLC"}
    assert result["brandId"] == "brand-123"
    assert result["status"] == "PENDING"


async def test_create_brand_forwards_payload_verbatim():
    """No field the caller did not send is ever invented or added."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"brandId": "brand-1"})

    client, http = make_client(handler)
    payload = {"entityType": "PRIVATE_PROFIT", "displayName": "Acme LLC"}
    async with http:
        await client.create_brand(payload)

    assert seen["body"] == payload
    assert set(seen["body"]) == {"entityType", "displayName"}


async def test_get_brand_looks_up_flat_response_and_quotes_brand_id():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["raw_path"] = request.url.raw_path
        # Flat top-level object - no "data" envelope.
        return httpx.Response(
            200,
            json={"brandId": "brand/with slash", "status": "OK"},
        )

    client, http = make_client(handler)
    async with http:
        result = await client.get_brand("brand/with slash")

    assert seen["method"] == "GET"
    assert b"/v2/10dlc/brand/brand%2Fwith%20slash" in seen["raw_path"]
    assert result["status"] == "OK"


async def test_create_campaign_uses_campaign_builder_path():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"campaignId": "camp-1", "status": "PENDING"})

    client, http = make_client(handler)
    async with http:
        # Telnyx's campaign field is lowercase "usecase".
        result = await client.create_campaign({"brandId": "brand-1", "usecase": "MIXED"})

    assert seen["method"] == "POST"
    assert seen["path"] == "/v2/10dlc/campaignBuilder"
    assert seen["body"] == {"brandId": "brand-1", "usecase": "MIXED"}
    assert result["campaignId"] == "camp-1"


async def test_get_campaign_returns_flat_status():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/10dlc/campaign/camp-1"
        return httpx.Response(200, json={"campaignId": "camp-1", "status": "OK"})

    client, http = make_client(handler)
    async with http:
        result = await client.get_campaign("camp-1")

    assert result["campaignId"] == "camp-1"
    assert result["status"] == "OK"


async def test_assign_phone_number_posts_to_phone_number_campaigns():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "phoneNumber": "+12145550123",
                "campaignId": "camp-1",
                "assignmentStatus": "ASSIGNED",
            },
        )

    client, http = make_client(handler)
    async with http:
        result = await client.assign_phone_number(
            {"phoneNumber": "+12145550123", "campaignId": "camp-1"}
        )

    assert seen["method"] == "POST"
    # Underscore/plural association path, NOT camelCase.
    assert seen["path"] == "/v2/10dlc/phone_number_campaigns"
    assert seen["body"] == {"phoneNumber": "+12145550123", "campaignId": "camp-1"}
    assert result["assignmentStatus"] == "ASSIGNED"


async def test_empty_payload_is_rejected_without_a_request():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"brandId": "brand-1"})

    client, http = make_client(handler)
    async with http:
        with pytest.raises(ValidationFailedError):
            await client.create_brand({})

    assert calls["n"] == 0


# ==================================================================================
# HTTP failures: correct error class, and no sensitive text leaked
# ==================================================================================
async def test_4xx_raises_validation_failed_without_leaking_sensitive_text():
    leaked = "EIN 12-3456789 did not match company name"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"errors": [{"code": "40001", "title": "Bad Request", "detail": leaked}]},
        )

    client, http = make_client(handler)
    async with http:
        with pytest.raises(ValidationFailedError) as excinfo:
            await client.create_brand({"ein": "12-3456789", "displayName": "Acme LLC"})

    message = str(excinfo.value)
    assert "422" in message
    assert "40001" in message
    assert leaked not in message
    assert "12-3456789" not in message
    assert "test-telnyx-key" not in message


async def test_401_and_403_raise_validation_failed():
    async def run(code: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                code, json={"errors": [{"code": "10009", "title": "Denied"}]}
            )

        client, http = make_client(handler)
        async with http:
            with pytest.raises(ValidationFailedError):
                await client.get_brand("brand-1")

    await run(401)
    await run(403)


async def test_429_is_retryable_feature_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"errors": [{"code": "10002", "detail": "slow down"}]}
        )

    client, http = make_client(handler)
    async with http:
        with pytest.raises(FeatureUnavailableError):
            await client.get_campaign("camp-1")


async def test_5xx_raises_feature_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errors": [{"code": "10009", "title": "boom"}]})

    client, http = make_client(handler)
    async with http:
        with pytest.raises(FeatureUnavailableError):
            await client.create_brand({"displayName": "Acme LLC"})


# ==================================================================================
# Malformed 2xx responses must not be mistaken for success
# ==================================================================================
async def test_200_with_non_json_body_raises_feature_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html>not json</html>",
            headers={"Content-Type": "text/html"},
        )

    client, http = make_client(handler)
    async with http:
        with pytest.raises(FeatureUnavailableError):
            await client.create_brand({"displayName": "Acme LLC"})


async def test_200_with_non_object_json_raises_feature_unavailable():
    """10DLC success bodies are flat OBJECTS - a bare JSON array is malformed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["unexpected"])

    client, http = make_client(handler)
    async with http:
        with pytest.raises(FeatureUnavailableError):
            await client.create_brand({"displayName": "Acme LLC"})


async def test_200_without_brand_id_raises_validation_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "PENDING"})

    client, http = make_client(handler)
    async with http:
        with pytest.raises(ValidationFailedError) as excinfo:
            await client.create_brand({"displayName": "Acme LLC"})

    assert "brand" in str(excinfo.value)


# ==================================================================================
# Transport failures and the POST-timeout rule (never auto-retried)
# ==================================================================================
async def test_transport_error_raises_feature_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client, http = make_client(handler)
    async with http:
        with pytest.raises(FeatureUnavailableError, match="unreachable"):
            await client.create_brand({"displayName": "Acme LLC"})


async def test_post_timeout_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client, http = make_client(handler)
    async with http:
        with pytest.raises(FeatureUnavailableError, match="not retried"):
            await client.create_brand({"displayName": "Acme LLC"})

    # A retry would double-submit a brand - exactly one attempt was made.
    assert calls["n"] == 1


# ==================================================================================
# Credential handling and injectable transport
# ==================================================================================
async def test_api_key_is_only_in_the_bearer_header():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["raw_path"] = request.url.raw_path
        return httpx.Response(200, json={"brandId": "brand-1"})

    client, http = make_client(handler)
    async with http:
        await client.create_brand({"displayName": "Acme LLC"})

    assert seen["auth"] == "Bearer test-telnyx-key"
    assert b"test-telnyx-key" not in seen["raw_path"]


async def test_base_url_is_configurable():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"brandId": "brand-1"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TelnyxRegistrationClient(
        api_key="test-telnyx-key", base_url="https://example.test/v2/", client=http
    )
    async with http:
        await client.create_brand({"displayName": "Acme LLC"})

    assert seen["url"] == "https://example.test/v2/10dlc/brand"


async def test_injected_client_is_not_closed_by_aclose():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"brandId": "brand-1"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TelnyxRegistrationClient(api_key="test-telnyx-key", client=http)

    await client.aclose()

    # The caller owns the injected client - aclose() must leave it usable.
    assert http.is_closed is False
    await http.aclose()
