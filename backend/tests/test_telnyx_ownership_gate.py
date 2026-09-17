"""CSaaS Telnyx ownership hard gate: an untagged number is never ours.

CSaaS and the CRM share ONE Telnyx account and ONE account-wide key, and Telnyx enforces
nothing between them. So "the account has this number" is not "CSaaS has this number"; the
gate is the csaas* tag, applied fail-closed on lookup and release, and stamped on order.

The load-bearing assertion is the release case: refusing must send ZERO DELETEs, because a
released CRM number is gone for good.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.errors import ValidationFailedError
from app.providers.telnyx.adapter import TelnyxMessagingCarrier
from app.providers.telnyx.numbers import is_csaas_owned

LOCAL = "+12145550100"


async def _no_sleep(_seconds: float) -> None:
    return None


def test_is_csaas_owned_accepts_prefixed_tags():
    assert is_csaas_owned({"tags": ["csaas"]}) is True
    assert is_csaas_owned({"tags": ["x", "CSaaS-prod"]}) is True


@pytest.mark.parametrize(
    "row",
    [
        {"tags": []},
        {"tags": ["crm"]},
        {},
        {"tags": None},
        None,
        {"tags": "csaas"},
    ],
)
def test_is_csaas_owned_rejects_everything_else(row):
    assert is_csaas_owned(row) is False


def _rows_handler(rows, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status)
        return httpx.Response(200, json={"data": rows})

    return handler


async def _lookup(handler, e164: str = LOCAL):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        carrier = TelnyxMessagingCarrier(api_key="k", client=client)
        return await carrier.lookup_owned_number(e164)


async def test_lookup_true_when_row_tagged():
    handler = _rows_handler([{"id": "pn-1", "phone_number": LOCAL, "tags": ["csaas"]}])
    assert await _lookup(handler) is True


async def test_lookup_false_when_row_untagged():
    handler = _rows_handler([{"id": "pn-1", "phone_number": LOCAL, "tags": ["crm"]}])
    assert await _lookup(handler) is False


async def test_lookup_false_when_no_rows():
    handler = _rows_handler([])
    assert await _lookup(handler) is False


async def test_lookup_none_on_server_error():
    handler = _rows_handler([], status=500)
    assert await _lookup(handler) is None


async def test_lookup_none_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("telnyx down")

    assert await _lookup(handler) is None


async def test_release_refuses_untagged_without_deleting():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": [{"id": "pn-crm", "phone_number": LOCAL, "tags": ["crm"]}]},
            )
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        carrier = TelnyxMessagingCarrier(api_key="k", client=client)
        with pytest.raises(ValidationFailedError):
            await carrier.release_number(LOCAL)

    assert [r.method for r in seen] == ["GET"]


async def test_release_deletes_when_tagged():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": [{"id": "pn-csaas", "phone_number": LOCAL, "tags": ["csaas"]}]},
            )
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        carrier = TelnyxMessagingCarrier(api_key="k", client=client)
        await carrier.release_number(LOCAL)

    deletes = [r for r in seen if r.method == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0].url.path.endswith("/phone_numbers/pn-csaas")


async def test_tag_as_csaas_patches_existing_tags_plus_csaas(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    patched: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": [{"id": "pn-1", "phone_number": LOCAL, "tags": ["crm"]}]},
            )
        patched.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {"id": "pn-1"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        carrier = TelnyxMessagingCarrier(api_key="k", client=client)
        ok = await carrier._tag_as_csaas(LOCAL)

    assert ok is True
    assert patched == [{"tags": ["crm", "csaas"]}]


async def test_tag_as_csaas_returns_false_on_patch_failure(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"data": [{"id": "pn-1", "phone_number": LOCAL, "tags": []}]},
            )
        return httpx.Response(422, json={"errors": [{"detail": "invalid tags"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        carrier = TelnyxMessagingCarrier(api_key="k", client=client)
        ok = await carrier._tag_as_csaas(LOCAL)

    assert ok is False
