"""Offline transport tests for the read-only toll-free verification list call.

Every test injects an ``httpx.AsyncClient`` backed by ``httpx.MockTransport``, so the
real Telnyx API is never contacted and the results are deterministic.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.errors import FeatureUnavailableError, ValidationFailedError
from app.providers.telnyx.tollfree_verification import TelnyxTollfreeVerificationClient

API_KEY = "KEY-tollfree-list-secret"
BASE_URL = "https://api.telnyx.test/v2"
LIST_PATH = "/v2/messaging_tollfree/verification/requests"

Handler = Callable[[httpx.Request], httpx.Response]

OK_BODY = {"records": [{"id": "req-1", "phone_number": "+18005551234"}], "total_records": 1}

# Bodies that are not JSON objects at all: the transport cannot attribute the problem
# to the caller, so these fail closed as an unavailable feature.
NON_OBJECT_BODIES = [
    (b"[]", "application/json"),
    (b'"records"', "application/json"),
    (b"42", "application/json"),
    (b"null", "application/json"),
    (b"true", "application/json"),
    (b"<html>not json</html>", "text/html"),
]
NON_OBJECT_IDS = [
    "json-list",
    "json-string",
    "json-number",
    "json-null",
    "json-bool",
    "not-json",
]

# JSON objects whose ``records`` / ``total_records`` violate the published contract:
# caller-visible validation failures, not transport failures.
INVALID_OBJECT_BODIES = [
    {"total_records": 1},
    {"records": []},
    {"records": "req-1", "total_records": 1},
    {"records": {"id": "req-1"}, "total_records": 1},
    {"records": ["req-1"], "total_records": 1},
    {"records": [None], "total_records": 1},
    {"records": [123], "total_records": 1},
    {"records": [], "total_records": -1},
    {"records": [], "total_records": 1.0},
    {"records": [], "total_records": 1.5},
    {"records": [], "total_records": "1"},
    {"records": [], "total_records": None},
    {"records": [], "total_records": True},
]
INVALID_OBJECT_IDS = [
    "missing-records",
    "missing-total-records",
    "records-not-list",
    "records-dict",
    "record-entry-not-dict",
    "record-entry-none",
    "record-entry-number",
    "total-records-negative",
    "total-records-float",
    "total-records-fraction",
    "total-records-string",
    "total-records-none",
    "total-records-bool",
]

INVALID_PAGING = [
    {"page": 0},
    {"page": -3},
    {"page": "1"},
    {"page": 1.5},
    {"page": True},
    {"page": False},
    {"page_size": 0},
    {"page_size": -1},
    {"page_size": "25"},
    {"page_size": 2.5},
    {"page_size": True},
    {"page_size": False},
]
INVALID_PAGING_IDS = [
    "page-zero",
    "page-negative",
    "page-string",
    "page-float",
    "page-true",
    "page-false",
    "page-size-zero",
    "page-size-negative",
    "page-size-string",
    "page-size-float",
    "page-size-true",
    "page-size-false",
]

BLANK_FILTERS = [
    {"phone_number": "", "business_name": "", "date_start": ""},
    {"phone_number": "   ", "business_name": "\t", "date_start": " "},
    {"phone_number": "\n", "business_name": "", "date_start": "\t\t"},
]
BLANK_FILTER_IDS = ["empty-strings", "whitespace-only", "mixed-blank"]


def _client(handler: Handler) -> tuple[TelnyxTollfreeVerificationClient, httpx.AsyncClient]:
    """Build a transport client around an injected MockTransport-backed client."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelnyxTollfreeVerificationClient(api_key=API_KEY, base_url=BASE_URL, client=http), http


def _never_called(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
    raise AssertionError("no HTTP request expected")


async def test_list_requests_sends_get_with_filters_and_returns_body() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    client, http = _client(handler)
    try:
        result = await client.list_requests(
            page=1,
            page_size=2,
            phone_number="+18005551234",
            business_name="Acme&Co",
            date_start="2024-01-02",
        )
    finally:
        await http.aclose()

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "GET"
    assert request.url.scheme == "https"
    assert request.url.host == "api.telnyx.test"
    assert request.url.path == LIST_PATH
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.content == b""

    # ``+`` and ``&`` must be percent-encoded, never left raw or turned into a query
    # separator by the encoder.
    raw_query = request.url.query.decode("ascii")
    assert "phone_number=%2B18005551234" in raw_query
    assert "business_name=Acme%26Co" in raw_query
    assert dict(request.url.params) == {
        "page": "1",
        "page_size": "2",
        "phone_number": "+18005551234",
        "business_name": "Acme&Co",
        "date_start": "2024-01-02",
    }
    assert result == OK_BODY


@pytest.mark.parametrize("filters", BLANK_FILTERS, ids=BLANK_FILTER_IDS)
async def test_list_requests_omits_blank_optional_filters(filters: dict) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"records": [], "total_records": 0})

    client, http = _client(handler)
    try:
        result = await client.list_requests(page=3, page_size=25, **filters)
    finally:
        await http.aclose()

    assert result == {"records": [], "total_records": 0}
    assert len(seen) == 1
    assert dict(seen[0].url.params) == {"page": "3", "page_size": "25"}
    assert seen[0].url.path == LIST_PATH


@pytest.mark.parametrize("overrides", INVALID_PAGING, ids=INVALID_PAGING_IDS)
async def test_invalid_paging_fails_before_any_request(overrides: dict) -> None:
    client, http = _client(_never_called)
    params = {"page": 1, "page_size": 25}
    params.update(overrides)
    try:
        with pytest.raises(ValidationFailedError):
            await client.list_requests(**params)
    finally:
        await http.aclose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"phone_number": 18005551234},
        {"phone_number": b"+18005551234"},
        {"business_name": 42},
        {"business_name": ["Acme"]},
        {"date_start": 20240102},
        {"date_start": {"iso": "2024-01-02"}},
    ],
    ids=[
        "phone-number-int",
        "phone-number-bytes",
        "business-name-int",
        "business-name-list",
        "date-start-int",
        "date-start-dict",
    ],
)
async def test_non_string_optional_filters_fail_before_any_request(overrides: dict) -> None:
    client, http = _client(_never_called)
    params = {"page": 1, "page_size": 25}
    params.update(overrides)
    try:
        with pytest.raises(ValidationFailedError):
            await client.list_requests(**params)
    finally:
        await http.aclose()


@pytest.mark.parametrize("payload", NON_OBJECT_BODIES, ids=NON_OBJECT_IDS)
async def test_non_object_body_fails_closed_as_unavailable(payload: tuple[bytes, str]) -> None:
    content, content_type = payload

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=content, headers={"content-type": content_type})

    client, http = _client(handler)
    try:
        with pytest.raises(FeatureUnavailableError):
            await client.list_requests(page=1, page_size=25)
    finally:
        await http.aclose()


@pytest.mark.parametrize("body", INVALID_OBJECT_BODIES, ids=INVALID_OBJECT_IDS)
async def test_invalid_object_body_fails_closed_as_validation(body: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client, http = _client(handler)
    try:
        with pytest.raises(ValidationFailedError):
            await client.list_requests(page=1, page_size=25)
    finally:
        await http.aclose()


async def test_injected_client_stays_open_and_usable() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"records": [], "total_records": 0})

    client, http = _client(handler)
    try:
        await client.list_requests(page=1, page_size=25)
        assert http.is_closed is False

        # An injected client is owned by the caller and must survive aclose().
        await client.aclose()
        assert http.is_closed is False

        await client.list_requests(page=2, page_size=25)
        assert http.is_closed is False
    finally:
        await http.aclose()

    assert [request.url.params["page"] for request in seen] == ["1", "2"]
