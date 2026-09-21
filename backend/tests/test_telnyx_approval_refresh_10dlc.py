"""Offline route tests for 10DLC approval evidence and the ``/refresh-telnyx`` routes.

Approval evidence (``carrier_refs["telnyx_approval"]``) is written by exactly two paths:
the fail-closed ``/status`` approval that FIRST moves a record to ``approved`` (source
``status_decision``) and the READ-ONLY ``/refresh-telnyx`` route (source ``refresh``,
which may also record ``revoked`` evidence). Every test drives the real routes over an
``httpx.MockTransport`` installed on ``app.state.telnyx_http_client``; the transport
records each request and refuses anything but a GET, so a refresh that tried to file would
fail. No socket is opened and no live carrier is called.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest

from app.compliance import telnyx_approval
from app.main import create_app
from app.models.numbers import Brand, Campaign
from tests.conftest import (
    TEST_PLATFORM_OPS_TOKEN,
    auth_headers,
    create_org,
    make_settings,
    register_and_login,
)

OPS_HEADER = "X-Platform-Ops-Token"
#: Non-empty so ``_telnyx_api_key`` passes; the mock transport reaches nothing.
TELNYX_KEY = "test-telnyx-key-not-real"
#: carrier_refs slot the two approval-evidence paths write into.
EVIDENCE_KEY = "telnyx_approval"
BRAND_REF = "brand-11111111-2222-3333-4444-555555555555"
CAMPAIGN_REF = "40000000-0000-4000-8000-000000000001"
#: A different record's identifier, to prove a mismatched payload is refused.
OTHER_REF = "40000000-0000-4000-8000-0000000000ff"
#: A deliberately old timestamp so a refresh's new ``checked_at`` is unambiguously newer.
OLD_TIME = datetime(2020, 1, 1, tzinfo=timezone.utc)
BRANDS_PATH = "/api/v1/registration/brands"
CAMPAIGNS_PATH = "/api/v1/registration/campaigns"


@pytest.fixture(autouse=True)
def no_live_http(monkeypatch):
    """Fail loudly if any path ever tries a real carrier call."""

    async def _blocked(self, request):  # noqa: ANN001
        raise AssertionError(f"live network call attempted: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked)


class _TelnyxStub:
    """Scripted Telnyx transport: records requests, answers 200, refuses non-GETs."""

    def __init__(self) -> None:
        self.payload: dict = {}
        self.fail: Exception | None = None
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # A refresh must never file; a POST/PUT here fails the surrounding test.
        assert request.method == "GET", f"unexpected {request.method} to {request.url}"
        if self.fail is not None:
            raise self.fail
        return httpx.Response(200, json=self.payload)


@pytest.fixture
async def api(engine):
    """(client, stub) - the app's Telnyx calls all flow through the injected mock."""
    application = create_app(make_settings(telnyx_api_key=TELNYX_KEY))
    stub = _TelnyxStub()
    injected = httpx.AsyncClient(transport=httpx.MockTransport(stub.handler))
    application.state.telnyx_http_client = injected
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        yield client, stub
    await injected.aclose()


async def _owner(client, email: str):
    token = await register_and_login(client, email)
    org = await create_org(client, token, email)
    return token, org


def _headers(token: str, org_id, *, ops: bool = True) -> dict:
    headers = auth_headers(token, org_id)
    if ops:
        headers[OPS_HEADER] = TEST_PLATFORM_OPS_TOKEN
    return headers


async def _create_brand(client, headers, name: str = "Evidence Brand") -> dict:
    r = await client.post(BRANDS_PATH, json={"name": name}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def _create_campaign(client, headers, brand_id: str) -> dict:
    r = await client.post(
        CAMPAIGNS_PATH,
        json={"brand_id": brand_id, "name": "Evidence Campaign"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _seed(session, model, row_id: str, org_id, *, ref=None, status=None, evidence=None):
    """Pin the tenant context, then set telnyx ref / status / evidence on the record."""
    session.info["org_id"] = uuid.UUID(str(org_id))
    try:
        row = await session.get(model, uuid.UUID(str(row_id)))
        assert row is not None
        refs = dict(row.carrier_refs or {})
        if ref is not None:
            refs["telnyx"] = ref
        if evidence is not None:
            refs[EVIDENCE_KEY] = evidence
        row.carrier_refs = refs
        if status is not None:
            row.status = status
        await session.commit()
    finally:
        session.info.pop("org_id", None)


def _approved_evidence(carrier_id: str, *, source: str | None = None) -> dict:
    return telnyx_approval.build_evidence(
        state=telnyx_approval.STATE_APPROVED,
        carrier_id=carrier_id,
        checked_at=OLD_TIME,
        source=source or telnyx_approval.SOURCE_STATUS_DECISION,
    )


def _evidence(body: dict) -> dict:
    return body["carrier_refs"][EVIDENCE_KEY]


def _checked_at(evidence: dict) -> datetime:
    parsed = datetime.fromisoformat(evidence["checked_at"])
    assert parsed.tzinfo is not None and parsed.utcoffset() is not None
    return parsed


async def _row(client, headers, path: str, row_id: str) -> dict:
    r = await client.get(path, headers=headers)
    assert r.status_code == 200, r.text
    return next(row for row in r.json() if row["id"] == row_id)


async def _approve(client, headers, path: str, row_id: str) -> httpx.Response:
    return await client.post(
        f"{path}/{row_id}/status", json={"status": "approved"}, headers=headers
    )


async def _refresh(client, headers, path: str, row_id: str) -> httpx.Response:
    return await client.post(f"{path}/{row_id}/refresh-telnyx", headers=headers)


# ----------------------------------------------------------------------------------
# /status approval writes status_decision evidence
# ----------------------------------------------------------------------------------
async def test_brand_approval_records_status_decision_evidence(api, session):
    client, stub = api
    token, org = await _owner(client, "evidence-brand@example.com")
    headers = _headers(token, org["id"])
    brand = await _create_brand(client, headers)
    await _seed(session, Brand, brand["id"], org["id"], ref=BRAND_REF)
    stub.payload = {"brandId": BRAND_REF, "status": "OK", "identityStatus": "VERIFIED"}

    r = await _approve(client, headers, BRANDS_PATH, brand["id"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    assert body["carrier_refs"]["telnyx"] == BRAND_REF
    ev = _evidence(body)
    assert ev["state"] == telnyx_approval.STATE_APPROVED
    assert ev["source"] == telnyx_approval.SOURCE_STATUS_DECISION
    assert ev["carrier_id"] == BRAND_REF
    assert _checked_at(ev)  # timezone-aware


async def test_campaign_approval_records_status_decision_evidence(api, session):
    client, stub = api
    token, org = await _owner(client, "evidence-campaign@example.com")
    headers = _headers(token, org["id"])
    brand = await _create_brand(client, headers)
    campaign = await _create_campaign(client, headers, brand["id"])
    await _seed(session, Campaign, campaign["id"], org["id"], ref=CAMPAIGN_REF)
    stub.payload = {"campaignId": CAMPAIGN_REF, "campaignStatus": "MNO_PROVISIONED"}

    r = await _approve(client, headers, CAMPAIGNS_PATH, campaign["id"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    ev = _evidence(body)
    assert ev["state"] == telnyx_approval.STATE_APPROVED
    assert ev["source"] == telnyx_approval.SOURCE_STATUS_DECISION
    assert ev["carrier_id"] == CAMPAIGN_REF
    assert _checked_at(ev)


async def test_replayed_approval_does_not_overwrite_checked_at(api, session):
    client, stub = api
    token, org = await _owner(client, "replay-brand@example.com")
    headers = _headers(token, org["id"])
    brand = await _create_brand(client, headers)
    await _seed(session, Brand, brand["id"], org["id"], ref=BRAND_REF)
    stub.payload = {"brandId": BRAND_REF, "status": "OK", "identityStatus": "VERIFIED"}

    first = await _approve(client, headers, BRANDS_PATH, brand["id"])
    assert first.status_code == 200, first.text
    first_checked = _evidence(first.json())["checked_at"]

    again = await _approve(client, headers, BRANDS_PATH, brand["id"])

    assert again.status_code == 409, again.text
    stored = await _row(client, headers, BRANDS_PATH, brand["id"])
    assert stored["status"] == "approved"
    assert _evidence(stored)["checked_at"] == first_checked


# ----------------------------------------------------------------------------------
# /refresh-telnyx guards and evidence updates
# ----------------------------------------------------------------------------------
async def test_refresh_is_operator_and_auth_guarded(api, session):
    client, stub = api
    token, org = await _owner(client, "guard-refresh@example.com")
    headers = _headers(token, org["id"])
    brand = await _create_brand(client, headers)
    await _seed(session, Brand, brand["id"], org["id"], ref=BRAND_REF, status="approved")
    stub.payload = {"brandId": BRAND_REF, "status": "OK", "identityStatus": "VERIFIED"}

    without_ops = await _refresh(
        client, _headers(token, org["id"], ops=False), BRANDS_PATH, brand["id"]
    )
    assert without_ops.status_code in (401, 403), without_ops.text

    without_auth = await client.post(
        f"{BRANDS_PATH}/{brand['id']}/refresh-telnyx",
        headers={OPS_HEADER: TEST_PLATFORM_OPS_TOKEN},
    )
    assert without_auth.status_code in (401, 403), without_auth.text

    assert stub.requests == []  # neither refused call reached the carrier


async def test_approved_refresh_updates_evidence_via_get_only(api, session):
    client, stub = api
    token, org = await _owner(client, "refresh-approved@example.com")
    headers = _headers(token, org["id"])
    brand = await _create_brand(client, headers)
    await _seed(
        session,
        Brand,
        brand["id"],
        org["id"],
        ref=BRAND_REF,
        status="approved",
        evidence=_approved_evidence(BRAND_REF),
    )
    before = _evidence(await _row(client, headers, BRANDS_PATH, brand["id"]))
    stub.payload = {"brandId": BRAND_REF, "status": "OK", "identityStatus": "VERIFIED"}

    r = await _refresh(client, headers, BRANDS_PATH, brand["id"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    ev = _evidence(body)
    assert ev["state"] == telnyx_approval.STATE_APPROVED
    assert ev["source"] == telnyx_approval.SOURCE_REFRESH
    assert _checked_at(ev) > OLD_TIME
    assert ev["checked_at"] != before["checked_at"]
    assert stub.requests and {req.method for req in stub.requests} == {"GET"}


async def test_non_approved_refresh_revokes_brand_evidence(api, session):
    client, stub = api
    token, org = await _owner(client, "revoke-brand@example.com")
    headers = _headers(token, org["id"])
    brand = await _create_brand(client, headers)
    await _seed(
        session,
        Brand,
        brand["id"],
        org["id"],
        ref=BRAND_REF,
        status="approved",
        evidence=_approved_evidence(BRAND_REF, source=telnyx_approval.SOURCE_REFRESH),
    )
    stub.payload = {"brandId": BRAND_REF, "status": "PENDING"}

    r = await _refresh(client, headers, BRANDS_PATH, brand["id"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"  # refresh never changes the local status
    ev = _evidence(body)
    assert ev["state"] == telnyx_approval.STATE_REVOKED
    assert ev["source"] == telnyx_approval.SOURCE_REFRESH


async def test_non_approved_refresh_revokes_campaign_evidence(api, session):
    client, stub = api
    token, org = await _owner(client, "revoke-campaign@example.com")
    headers = _headers(token, org["id"])
    brand = await _create_brand(client, headers)
    campaign = await _create_campaign(client, headers, brand["id"])
    await _seed(
        session,
        Campaign,
        campaign["id"],
        org["id"],
        ref=CAMPAIGN_REF,
        status="approved",
        evidence=_approved_evidence(CAMPAIGN_REF),
    )
    stub.payload = {"campaignId": CAMPAIGN_REF, "campaignStatus": "PENDING"}

    r = await _refresh(client, headers, CAMPAIGNS_PATH, campaign["id"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    ev = _evidence(body)
    assert ev["state"] == telnyx_approval.STATE_REVOKED
    assert ev["source"] == telnyx_approval.SOURCE_REFRESH


async def test_refresh_failure_and_mismatch_leave_evidence_untouched(api, session):
    client, stub = api
    token, org = await _owner(client, "untouched@example.com")
    headers = _headers(token, org["id"])
    brand = await _create_brand(client, headers)
    await _seed(
        session,
        Brand,
        brand["id"],
        org["id"],
        ref=BRAND_REF,
        status="approved",
        evidence=_approved_evidence(BRAND_REF),
    )
    before = _evidence(await _row(client, headers, BRANDS_PATH, brand["id"]))

    stub.fail = httpx.ConnectError("carrier down")
    failed = await _refresh(client, headers, BRANDS_PATH, brand["id"])
    assert failed.status_code == 409, failed.text
    assert _evidence(await _row(client, headers, BRANDS_PATH, brand["id"])) == before

    stub.fail = None
    stub.payload = {"brandId": OTHER_REF, "status": "OK", "identityStatus": "VERIFIED"}
    mismatched = await _refresh(client, headers, BRANDS_PATH, brand["id"])
    assert mismatched.status_code == 409, mismatched.text
    assert _evidence(await _row(client, headers, BRANDS_PATH, brand["id"])) == before
