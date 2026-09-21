"""Offline route tests for the carrier-confirmed approval guards.

``POST /api/v1/registration/campaigns/{id}/status`` and
``POST /api/v1/registration/tollfree/{id}/status`` accept an ``approved`` registrar
decision only when a fresh Telnyx GET confirms the SAME record and reports it approved.
Every test drives the real route over an ``httpx.MockTransport`` installed on
``app.state.telnyx_http_client``; no socket is opened and no live carrier is called.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from app.db.session import get_sessionmaker
from app.main import create_app
from app.models.numbers import Campaign, TollFreeVerification
from tests.conftest import TEST_PLATFORM_OPS_TOKEN, make_settings

#: Header the operator guard compares against ``settings.platform_ops_token``.
OPS_HEADER = "X-Platform-Ops-Token"
#: Non-empty so ``_telnyx_api_key`` passes; the mock transport means it reaches nothing.
TELNYX_KEY = "test-telnyx-key-not-real"
PASSWORD = "correct-horse-battery"
#: The Telnyx identifier stored on the local record; the carrier must echo it back.
TELNYX_REF = "40000000-0000-4000-8000-000000000001"
#: A different record's identifier, to prove a mismatched payload is refused.
OTHER_REF = "40000000-0000-4000-8000-0000000000ff"


@pytest.fixture
async def telnyx_app(engine, settings):
    """The ASGI app with a (never dialled) Telnyx key plus an HTTP client to drive it.

    ``install(payload)`` swaps in a fresh ``MockTransport``-backed client that answers
    every Telnyx request with ``payload``. The org has no active Telnyx account, so the
    guard resolves to these base settings and finds the key above. Built through
    ``make_settings`` so ``telnyx_api_key`` is validated into a ``SecretStr`` - the Telnyx
    client calls ``get_secret_value`` on it. ``allow_unverified_number_add`` is a
    test-only convenience so the TFV fixtures can create their number locally without a
    live Telnyx ownership lookup; it changes nothing in production.
    """
    application = create_app(
        make_settings(telnyx_api_key=TELNYX_KEY, allow_unverified_number_add=True)
    )
    opened: list[httpx.AsyncClient] = []

    def install(payload: dict) -> None:
        def _handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        injected = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        opened.append(injected)
        application.state.telnyx_http_client = injected

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        yield client, application, install

    for injected in opened:
        await injected.aclose()


def _headers(application, token: str, org_id) -> dict:
    # ``settings.platform_ops_token`` is a SecretStr; send the plain constant that
    # ``make_settings`` seeded it from rather than the masked object.
    return {
        "Authorization": f"Bearer {token}",
        "X-Org-Id": str(org_id),
        OPS_HEADER: TEST_PLATFORM_OPS_TOKEN,
    }


async def _register_and_org(client: httpx.AsyncClient, email: str) -> tuple[str, dict]:
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "full_name": "Tester"},
    )
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    r = await client.post(
        "/api/v1/orgs", json={"name": email}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 201, r.text
    return token, r.json()


async def _create_campaign(client, application, token: str, org: dict) -> dict:
    h = _headers(application, token, org["id"])
    r = await client.post(
        "/api/v1/registration/brands", json={"name": "Guard Brand"}, headers=h
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/v1/registration/campaigns",
        json={"brand_id": r.json()["id"], "name": "Guard Campaign"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _create_tollfree(client, application, token: str, org: dict) -> dict:
    h = _headers(application, token, org["id"])
    r = await client.post("/api/v1/numbers", json={"e164": "+18005550100"}, headers=h)
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/v1/registration/tollfree",
        json={"number_id": r.json()["id"], "business_name": "Guard Co"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _seed_ref(session, model, row_id: str, ref: str, org_id) -> None:
    """Write the Telnyx reference with the tenant context PINNED.

    The API's own session is scoped to an org; this hand-opened test session is not, so
    the row would be filtered out (or its write rejected) unless we pin the same org id.
    """
    session.info["org_id"] = uuid.UUID(str(org_id))
    try:
        row = await session.get(model, uuid.UUID(row_id))
        assert row is not None
        row.carrier_refs = {"telnyx": ref}
        await session.commit()
    finally:
        session.info.pop("org_id", None)


async def _fresh_status(model, row_id: str, org_id) -> str:
    """Read the persisted status back in a NEW session under the same tenant context."""
    org_uuid = uuid.UUID(str(org_id))
    async with get_sessionmaker()() as s:
        s.info["org_id"] = org_uuid
        try:
            row = await s.get(model, uuid.UUID(row_id))
            assert row is not None
            return row.status
        finally:
            s.info.pop("org_id", None)


async def _approve_campaign(client, application, token, org, campaign_id: str):
    return await client.post(
        f"/api/v1/registration/campaigns/{campaign_id}/status",
        json={"status": "approved"},
        headers=_headers(application, token, org["id"]),
    )


async def _approve_tfv(client, application, token, org, tfv_id: str):
    return await client.post(
        f"/api/v1/registration/tollfree/{tfv_id}/status",
        json={"status": "approved"},
        headers=_headers(application, token, org["id"]),
    )


# ----------------------------------------------------------------------------------
# Campaigns
# ----------------------------------------------------------------------------------
async def test_campaign_pending_carrier_refuses_approval(telnyx_app, session):
    client, application, install = telnyx_app
    token, org = await _register_and_org(client, "camp-pending@example.com")
    campaign = await _create_campaign(client, application, token, org)
    await _seed_ref(session, Campaign, campaign["id"], TELNYX_REF, org["id"])
    # Right campaign, but the carrier has not provisioned it yet.
    install({"campaignId": TELNYX_REF, "campaignStatus": "PENDING"})
    r = await _approve_campaign(client, application, token, org, campaign["id"])
    assert r.status_code == 409, r.text
    assert await _fresh_status(Campaign, campaign["id"], org["id"]) != "approved"


async def test_campaign_mismatched_id_refuses_approval(telnyx_app, session):
    client, application, install = telnyx_app
    token, org = await _register_and_org(client, "camp-mismatch@example.com")
    campaign = await _create_campaign(client, application, token, org)
    await _seed_ref(session, Campaign, campaign["id"], TELNYX_REF, org["id"])
    # Approved, but for a DIFFERENT campaign.
    install({"campaignId": OTHER_REF, "campaignStatus": "MNO_PROVISIONED"})
    r = await _approve_campaign(client, application, token, org, campaign["id"])
    assert r.status_code == 409, r.text
    assert await _fresh_status(Campaign, campaign["id"], org["id"]) != "approved"


async def test_campaign_carrier_confirmed_approval_succeeds(telnyx_app, session):
    client, application, install = telnyx_app
    token, org = await _register_and_org(client, "camp-ok@example.com")
    campaign = await _create_campaign(client, application, token, org)
    await _seed_ref(session, Campaign, campaign["id"], TELNYX_REF, org["id"])
    install({"campaignId": TELNYX_REF, "campaignStatus": "MNO_PROVISIONED"})
    r = await _approve_campaign(client, application, token, org, campaign["id"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"


# ----------------------------------------------------------------------------------
# Toll-free verification
# ----------------------------------------------------------------------------------
async def test_tfv_verified_confirmed_approval_succeeds(telnyx_app, session):
    client, application, install = telnyx_app
    token, org = await _register_and_org(client, "tfv-ok@example.com")
    tfv = await _create_tollfree(client, application, token, org)
    await _seed_ref(session, TollFreeVerification, tfv["id"], TELNYX_REF, org["id"])
    install({"id": TELNYX_REF, "verificationStatus": "Verified"})
    r = await _approve_tfv(client, application, token, org, tfv["id"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"


async def test_tfv_status_field_alone_refuses_approval(telnyx_app, session):
    client, application, install = telnyx_app
    token, org = await _register_and_org(client, "tfv-status-only@example.com")
    tfv = await _create_tollfree(client, application, token, org)
    await _seed_ref(session, TollFreeVerification, tfv["id"], TELNYX_REF, org["id"])
    # ``status`` is NOT the documented toll-free field; only ``verificationStatus``
    # counts, so an unrelated ``status: approved`` must confirm nothing.
    install({"id": TELNYX_REF, "status": "approved"})
    r = await _approve_tfv(client, application, token, org, tfv["id"])
    assert r.status_code == 409, r.text
    assert await _fresh_status(TollFreeVerification, tfv["id"], org["id"]) != "approved"


async def test_tfv_mismatched_id_refuses_approval(telnyx_app, session):
    client, application, install = telnyx_app
    token, org = await _register_and_org(client, "tfv-mismatch@example.com")
    tfv = await _create_tollfree(client, application, token, org)
    await _seed_ref(session, TollFreeVerification, tfv["id"], TELNYX_REF, org["id"])
    install({"id": OTHER_REF, "verificationStatus": "Verified"})
    r = await _approve_tfv(client, application, token, org, tfv["id"])
    assert r.status_code == 409, r.text
    assert await _fresh_status(TollFreeVerification, tfv["id"], org["id"]) != "approved"
