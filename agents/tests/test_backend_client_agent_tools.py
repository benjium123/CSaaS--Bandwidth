"""Phase 9: BackendClient methods for the agent-side tool seams (contact lookup,
appointment booking, KB search, handoff, AMD). Exercised against a real httpx.AsyncClient
wired to an httpx.MockTransport so the request URL, method, query params, JSON body, and
auth header are all verified exactly as the backend contract specifies - not just that
"some POST happened", the way the duck-typed fake in test_backend_client.py does for the
older transcript methods.
"""

from __future__ import annotations

import json

import httpx
import jwt
import pytest

from agents.backend_client import BackendClient


def _client_with_handler(handler) -> BackendClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="http://backend")
    return BackendClient(
        "http://backend", "test-key", "test-secret-32-bytes-long!!", client=http_client
    )


def _assert_auth(request: httpx.Request) -> None:
    auth = request.headers.get("authorization", "")
    assert auth.startswith("Bearer ")
    token = auth.removeprefix("Bearer ")
    claims = jwt.decode(token, "test-secret-32-bytes-long!!", algorithms=["HS256"])
    assert claims["iss"] == "test-key"


@pytest.mark.asyncio
async def test_get_contact_hits_path_with_e164_and_call_id_query_param() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        _assert_auth(request)
        return httpx.Response(
            200,
            json={
                "name": "Jane Doe",
                "tags": ["vip"],
                "last_messages": [{"direction": "inbound", "body": "hi", "at": "t"}],
            },
        )

    client = _client_with_handler(handler)
    result = await client.get_contact("call-1", "+15551234567")

    assert str(seen["url"].path) == "/api/v1/agent/contact/+15551234567"
    assert seen["url"].params["call_id"] == "call-1"
    assert result == {
        "name": "Jane Doe",
        "tags": ["vip"],
        "last_messages": [{"direction": "inbound", "body": "hi", "at": "t"}],
    }


@pytest.mark.asyncio
async def test_get_contact_returns_none_on_non_200() -> None:
    client = _client_with_handler(lambda request: httpx.Response(404))
    assert await client.get_contact("call-1", "+15551234567") is None


@pytest.mark.asyncio
async def test_get_contact_returns_none_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client_with_handler(handler)
    assert await client.get_contact("call-1", "+15551234567") is None


@pytest.mark.asyncio
async def test_book_appointment_posts_exact_payload_shape() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        seen["json"] = json.loads(request.content)
        _assert_auth(request)
        return httpx.Response(
            201,
            json={
                "id": "appt-1",
                "raw_when": "tomorrow at 3",
                "scheduled_for": None,
                "status": "booked",
            },
        )

    client = _client_with_handler(handler)
    result = await client.book_appointment(
        "call-1", "+15551234567", "tomorrow at 3", "roof inspection"
    )

    assert str(seen["url"].path) == "/api/v1/agent/appointments"
    assert seen["json"] == {
        "call_id": "call-1",
        "contact_e164": "+15551234567",
        "raw_when": "tomorrow at 3",
        "notes": "roof inspection",
    }
    assert result == {
        "id": "appt-1",
        "raw_when": "tomorrow at 3",
        "scheduled_for": None,
        "status": "booked",
    }


@pytest.mark.asyncio
async def test_book_appointment_returns_none_on_non_201() -> None:
    client = _client_with_handler(lambda request: httpx.Response(500))
    result = await client.book_appointment("call-1", "+15551234567", "tomorrow", "")
    assert result is None


@pytest.mark.asyncio
async def test_book_appointment_returns_none_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client_with_handler(handler)
    result = await client.book_appointment("call-1", "+15551234567", "tomorrow", "")
    assert result is None


@pytest.mark.asyncio
async def test_kb_search_sends_call_id_and_q_query_params() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        _assert_auth(request)
        return httpx.Response(
            200,
            json={"chunks": [{"title": "Hours", "text": "9-5", "score": 0.9}]},
        )

    client = _client_with_handler(handler)
    chunks = await client.kb_search("call-1", "what are your hours")

    assert str(seen["url"].path) == "/api/v1/agent/kb/search"
    assert seen["url"].params["call_id"] == "call-1"
    assert seen["url"].params["q"] == "what are your hours"
    assert chunks == [{"title": "Hours", "text": "9-5", "score": 0.9}]


@pytest.mark.asyncio
async def test_kb_search_returns_empty_list_on_non_200() -> None:
    client = _client_with_handler(lambda request: httpx.Response(500))
    assert await client.kb_search("call-1", "q") == []


@pytest.mark.asyncio
async def test_kb_search_returns_empty_list_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client_with_handler(handler)
    assert await client.kb_search("call-1", "q") == []


@pytest.mark.asyncio
async def test_post_handoff_posts_exact_payload_shape_and_returns_true() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        seen["json"] = json.loads(request.content)
        _assert_auth(request)
        return httpx.Response(200, json={"published": True})

    client = _client_with_handler(handler)
    ok = await client.post_handoff("call-1", "caller wants a person", "user: hi\nagent: hello")

    assert str(seen["url"].path) == "/api/v1/agent/handoff"
    assert seen["json"] == {
        "call_id": "call-1",
        "reason": "caller wants a person",
        "summary": "user: hi\nagent: hello",
    }
    assert ok is True


@pytest.mark.asyncio
async def test_post_handoff_returns_false_on_non_2xx() -> None:
    client = _client_with_handler(lambda request: httpx.Response(500))
    assert await client.post_handoff("call-1", "reason", "summary") is False


@pytest.mark.asyncio
async def test_post_handoff_returns_false_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client_with_handler(handler)
    assert await client.post_handoff("call-1", "reason", "summary") is False


@pytest.mark.asyncio
async def test_post_amd_posts_exact_payload_shape_and_returns_true() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        seen["json"] = json.loads(request.content)
        _assert_auth(request)
        return httpx.Response(200)

    client = _client_with_handler(handler)
    ok = await client.post_amd("call-1", "machine")

    assert str(seen["url"].path) == "/api/v1/agent/amd"
    assert seen["json"] == {"call_id": "call-1", "result": "machine"}
    assert ok is True


@pytest.mark.asyncio
async def test_post_amd_returns_false_on_non_2xx() -> None:
    client = _client_with_handler(lambda request: httpx.Response(500))
    assert await client.post_amd("call-1", "human") is False


@pytest.mark.asyncio
async def test_post_amd_returns_false_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = _client_with_handler(handler)
    assert await client.post_amd("call-1", "human") is False
