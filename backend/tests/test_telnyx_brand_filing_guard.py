"""Guard tests: filing fails closed before credentials or any carrier call."""

from types import SimpleNamespace

import httpx
import pytest

from app.errors import ConflictError
from app.services import telnyx_brand_filing as filing


def _client(recorded: list) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json={"brandId": "TLX-1"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _invoke(brand, client):
    return await filing.file_brand_with_telnyx(
        session=object(),
        settings=object(),
        brand=brand,
        company_name="Acme",
        first_name="Ana",
        last_name="Lopez",
        brand_relationship="is",
        client=client,
    )


def _patch_lock(monkeypatch) -> None:
    async def fake_lock(session, brand):
        return brand

    monkeypatch.setattr(filing, "_lock_brand", fake_lock)


@pytest.mark.asyncio
async def test_refuses_approved_brand_before_carrier_call(monkeypatch):
    recorded: list = []
    brand = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        status="approved",
        carrier_refs={},
    )
    _patch_lock(monkeypatch)

    async with _client(recorded) as client:
        with pytest.raises(ConflictError):
            await _invoke(brand, client)

    assert recorded == []


@pytest.mark.asyncio
async def test_refuses_attempt_marker_on_submitted_brand(monkeypatch):
    recorded: list = []
    brand = SimpleNamespace(
        id="22222222-2222-2222-2222-222222222222",
        status="submitted",
        carrier_refs={filing._ATTEMPT_KEY: {"status": "pending"}},
    )
    _patch_lock(monkeypatch)
    monkeypatch.setattr(filing, "validate_brand_for_submission", lambda brand: None)

    async with _client(recorded) as client:
        with pytest.raises(ConflictError):
            await _invoke(brand, client)

    assert recorded == []
