"""Offline route tests for the fail-closed ``approved`` brand guard.

``POST /api/v1/registration/brands/{id}/status`` with ``status="approved"`` is only
accepted when the local brand already carries a Telnyx brand reference AND a fresh Telnyx
GET returns that same brand reporting an approved status. Every test injects an
``httpx.AsyncClient`` over a ``MockTransport`` via ``app.state.telnyx_http_client``, so the
route reuses it and no socket is opened; ``no_live_http`` fails the test outright if
anything tries a real connection anyway.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.main import create_app
from app.models.numbers import Brand
from tests.conftest import (
    TEST_PLATFORM_OPS_TOKEN,
    auth_headers,
    create_org,
    make_settings,
    register_and_login,
)

#: The Telnyx brand id we pretend the carrier assigned to the local brand.
BRAND_REF = "brand-11111111-2222-3333-4444-555555555555"
OPS_HEADERS = {"X-Platform-Ops-Token": TEST_PLATFORM_OPS_TOKEN}
EMAIL = "brand-approval-guard@example.com"


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    """Fail loudly if the guard ever tries a real carrier call."""

    async def _blocked(self, request):  # noqa: ANN001
        raise AssertionError(f"live network call attempted: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)


class _TelnyxStub:
    """Scripted Telnyx GET payload plus a record of the requests made."""

    def __init__(self) -> None:
        self.payload: dict = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.payload)


@pytest.fixture
def brand_settings():
    # A key must be resolvable or the guard refuses before it can confirm anything; the
    # value itself is never used because the transport is injected. database_url stays at
    # TEST_DATABASE_URL (make_settings' default), so this fixture's app binds to exactly
    # the engine the `engine`/`session` fixtures created.
    return make_settings(telnyx_api_key="test-telnyx-key")


@pytest.fixture
async def guard(engine, brand_settings):
    """(client, stub) - the app's Telnyx calls all flow through the injected mock."""
    application = create_app(brand_settings)
    stub = _TelnyxStub()
    telnyx_http = httpx.AsyncClient(transport=httpx.MockTransport(stub.handler))
    application.state.telnyx_http_client = telnyx_http
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, stub
    await telnyx_http.aclose()


async def _make_brand(client, session, ref: str | None) -> tuple[str, dict]:
    """Register an owner, create a brand, optionally seed carrier_refs['telnyx']."""
    token = await register_and_login(client, EMAIL)
    org = await create_org(client, token, "Brand Guard Org")
    headers = {**auth_headers(token, org["id"]), **OPS_HEADERS}
    r = await client.post(
        "/api/v1/registration/brands", json={"name": "Acme Co"}, headers=headers
    )
    assert r.status_code == 201, r.text
    brand_id = r.json()["id"]
    if ref is not None:
        # The ORM session is tenant-scoped: any read/write raises
        # MissingTenantContextError unless the owning org is pinned. Scope it for the seed
        # write only, and always drop it again so later operations in this session are
        # not silently scoped to a stale org.
        session.info["org_id"] = uuid.UUID(org["id"])
        try:
            brand = (
                await session.execute(
                    sa.select(Brand).where(Brand.id == uuid.UUID(brand_id))
                )
            ).scalar_one()
            brand.carrier_refs = {"telnyx": ref}
            await session.commit()
        finally:
            session.info.pop("org_id", None)
    return brand_id, headers


async def _approve(client, headers, brand_id: str) -> httpx.Response:
    return await client.post(
        f"/api/v1/registration/brands/{brand_id}/status",
        json={"status": "approved"},
        headers=headers,
    )


async def _stored_status(client, headers, brand_id: str) -> str:
    r = await client.get("/api/v1/registration/brands", headers=headers)
    assert r.status_code == 200, r.text
    return next(b["status"] for b in r.json() if b["id"] == brand_id)


async def test_approved_without_a_telnyx_ref_is_refused(guard, session):
    client, stub = guard
    brand_id, headers = await _make_brand(client, session, None)

    r = await _approve(client, headers, brand_id)

    assert r.status_code == 409, r.text
    assert stub.requests == []  # nothing to confirm against: no carrier call at all
    assert await _stored_status(client, headers, brand_id) != "approved"


async def test_carrier_still_pending_is_refused(guard, session):
    client, stub = guard
    brand_id, headers = await _make_brand(client, session, BRAND_REF)
    stub.payload = {
        "brandId": BRAND_REF,
        "status": "PENDING",
        "identityStatus": "UNVERIFIED",
    }

    r = await _approve(client, headers, brand_id)

    assert r.status_code == 409, r.text
    assert len(stub.requests) == 1
    assert BRAND_REF in str(stub.requests[0].url)
    assert await _stored_status(client, headers, brand_id) != "approved"


async def test_different_brand_id_from_carrier_is_refused(guard, session):
    client, stub = guard
    brand_id, headers = await _make_brand(client, session, BRAND_REF)
    stub.payload = {
        "brandId": "brand-99999999-other",
        "status": "OK",
        "identityStatus": "VERIFIED",
    }

    r = await _approve(client, headers, brand_id)

    assert r.status_code == 409, r.text
    assert await _stored_status(client, headers, brand_id) != "approved"


async def test_carrier_confirmed_approved_brand_is_recorded(guard, session):
    client, stub = guard
    brand_id, headers = await _make_brand(client, session, BRAND_REF)
    stub.payload = {
        "brandId": BRAND_REF,
        "status": "OK",
        "identityStatus": "VERIFIED",
    }

    r = await _approve(client, headers, brand_id)

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["carrier_refs"]["telnyx"] == BRAND_REF
    assert BRAND_REF in str(stub.requests[0].url)
    assert await _stored_status(client, headers, brand_id) == "approved"


async def test_repeating_a_confirmed_approval_is_a_conflict(guard, session):
    """A second identical approval must not be reported as a second success.

    The carrier still confirms the brand, but the local record is already approved, so
    the decision changes nothing: it is rolled back and refused rather than answered with
    a misleading 200, and the stored status stays approved.
    """
    client, stub = guard
    brand_id, headers = await _make_brand(client, session, BRAND_REF)
    stub.payload = {
        "brandId": BRAND_REF,
        "status": "OK",
        "identityStatus": "VERIFIED",
    }

    first = await _approve(client, headers, brand_id)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "approved"

    again = await _approve(client, headers, brand_id)

    assert again.status_code == 409, again.text
    assert len(stub.requests) == 2  # re-confirmed with the carrier before the no-op
    assert await _stored_status(client, headers, brand_id) == "approved"
