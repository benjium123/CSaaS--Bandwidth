"""Offline route tests for toll-free approval evidence and the refresh route.

``POST /api/v1/registration/tollfree/{id}/status`` records approval evidence ONLY on the
fail-closed transition that first moves a verification to ``approved``;
``POST /api/v1/registration/tollfree/{id}/refresh-telnyx`` re-polls Telnyx once and
refreshes (or revokes) that evidence without ever moving the local status. Every test
drives the real route over an ``httpx.MockTransport`` installed on
``app.state.telnyx_http_client``: no socket is opened and no live carrier is called.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.compliance import telnyx_approval
from app.db.session import get_sessionmaker
from app.main import create_app
from app.models.numbers import TollFreeVerification
from tests.conftest import TEST_PLATFORM_OPS_TOKEN, make_settings

#: Header the operator guard compares against ``settings.platform_ops_token``.
OPS_HEADER = "X-Platform-Ops-Token"
#: Non-empty so ``_telnyx_api_key`` passes; the mock transport means it reaches nothing.
TELNYX_KEY = "test-telnyx-key-not-real"
PASSWORD = "correct-horse-battery"
#: The Telnyx identifier stored on the local record; the carrier must echo it back.
TELNYX_REF = "40000000-0000-4000-8000-000000000001"
#: A different verification's identifier, to prove a mismatched payload is refused.
OTHER_REF = "40000000-0000-4000-8000-0000000000ff"
#: carrier_refs key under which approval evidence is bound (see registration.py).
APPROVAL_KEY = "telnyx_approval"
#: The documented toll-free payload the guard treats as approved. The exact-identifier
#: guard requires the carrier to echo back the reference on file, so the approved fixture
#: MUST carry ``id`` alongside the documented ``verificationStatus``; a payload that
#: omits the id confirms nothing and is refused.
VERIFIED = {"id": TELNYX_REF, "verificationStatus": "Verified"}


@pytest.fixture
async def telnyx_app(engine, settings):
    """The ASGI app with a (never dialled) Telnyx key plus a controllable HTTP client.

    ``install(payload)`` swaps in a fresh ``MockTransport``-backed client answering every
    Telnyx request with ``payload``; ``install(error=...)`` makes the transport raise so
    the fail-closed guard is exercised. Recorded requests are exposed so a test can prove
    the carrier is only ever reached with GET.
    """
    application = create_app(
        make_settings(telnyx_api_key=TELNYX_KEY, allow_unverified_number_add=True)
    )
    opened: list[httpx.AsyncClient] = []
    calls: list[httpx.Request] = []

    def install(payload=None, *, error=None, status_code=200) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if error is not None:
                raise error
            return httpx.Response(status_code, json=payload)

        injected = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        opened.append(injected)
        application.state.telnyx_http_client = injected

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        yield client, application, install, calls

    for injected in opened:
        await injected.aclose()


# ----------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------
def _headers(token: str, org_id) -> dict:
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
    r = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    r = await client.post(
        "/api/v1/orgs", json={"name": email}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 201, r.text
    return token, r.json()


async def _create_tollfree(client, token: str, org: dict) -> dict:
    r = await client.post(
        "/api/v1/numbers",
        json={"e164": "+18005550100"},
        headers=_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/api/v1/registration/tollfree",
        json={"number_id": r.json()["id"], "business_name": "Evidence Co"},
        headers=_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _seed(session, tfv_id: str, refs: dict, org_id, *, status: str | None = None) -> None:
    """Write carrier_refs (and optional status) with the tenant context PINNED.

    The hand-opened test session is not org-scoped, so the row would be filtered out (or
    the write rejected) unless we pin the same org id the API request uses.
    """
    session.info["org_id"] = uuid.UUID(str(org_id))
    try:
        row = await session.get(TollFreeVerification, uuid.UUID(tfv_id))
        assert row is not None
        row.carrier_refs = refs
        if status is not None:
            row.status = status
        await session.commit()
    finally:
        session.info.pop("org_id", None)


async def _snapshot(tfv_id: str, org_id) -> SimpleNamespace:
    """Read status + carrier_refs back in a NEW org-scoped session."""
    org_uuid = uuid.UUID(str(org_id))
    async with get_sessionmaker()() as s:
        s.info["org_id"] = org_uuid
        try:
            row = await s.get(TollFreeVerification, uuid.UUID(tfv_id))
            assert row is not None
            return SimpleNamespace(status=row.status, carrier_refs=dict(row.carrier_refs or {}))
        finally:
            s.info.pop("org_id", None)


def _checked_at(evidence: dict) -> datetime:
    raw = evidence["checked_at"]
    return raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))


async def _approve(client, token, org, tfv_id: str):
    return await client.post(
        f"/api/v1/registration/tollfree/{tfv_id}/status",
        json={"status": "approved"},
        headers=_headers(token, org["id"]),
    )


async def _refresh(client, token, org, tfv_id: str):
    return await client.post(
        f"/api/v1/registration/tollfree/{tfv_id}/refresh-telnyx",
        headers=_headers(token, org["id"]),
    )


# ----------------------------------------------------------------------------------
# Approval evidence written by the /status decision
# ----------------------------------------------------------------------------------
async def test_status_approval_records_bound_evidence(telnyx_app, session):
    client, _app, install, _calls = telnyx_app
    token, org = await _register_and_org(client, "tfv-evidence@example.com")
    tfv = await _create_tollfree(client, token, org)
    await _seed(session, tfv["id"], {"telnyx": TELNYX_REF}, org["id"])
    install(VERIFIED)

    r = await _approve(client, token, org, tfv["id"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"

    ev = (await _snapshot(tfv["id"], org["id"])).carrier_refs[APPROVAL_KEY]
    assert ev["state"] == telnyx_approval.STATE_APPROVED
    assert ev["source"] == telnyx_approval.SOURCE_STATUS_DECISION
    # Evidence is bound to the EXACT Telnyx identifier currently on file.
    assert TELNYX_REF in set(ev.values())
    assert _checked_at(ev).tzinfo is not None


async def test_status_replay_conflicts_without_replacing_evidence(telnyx_app, session):
    client, _app, install, _calls = telnyx_app
    token, org = await _register_and_org(client, "tfv-replay@example.com")
    tfv = await _create_tollfree(client, token, org)
    await _seed(session, tfv["id"], {"telnyx": TELNYX_REF}, org["id"])
    install(VERIFIED)

    first = await _approve(client, token, org, tfv["id"])
    assert first.status_code == 200, first.text
    before = (await _snapshot(tfv["id"], org["id"])).carrier_refs[APPROVAL_KEY]

    # A second identical decision changes nothing -> rolled back, never a misleading 200.
    replay = await _approve(client, token, org, tfv["id"])
    assert replay.status_code == 409, replay.text
    snap = await _snapshot(tfv["id"], org["id"])
    assert snap.status == "approved"
    assert snap.carrier_refs[APPROVAL_KEY] == before


# ----------------------------------------------------------------------------------
# Refresh route (read-only re-poll)
# ----------------------------------------------------------------------------------
async def test_verified_refresh_updates_approved_evidence(telnyx_app, session):
    client, _app, install, _calls = telnyx_app
    token, org = await _register_and_org(client, "tfv-refresh-verified@example.com")
    tfv = await _create_tollfree(client, token, org)
    stale = telnyx_approval.build_evidence(
        state=telnyx_approval.STATE_APPROVED,
        carrier_id=TELNYX_REF,
        checked_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        source=telnyx_approval.SOURCE_STATUS_DECISION,
    )
    await _seed(
        session,
        tfv["id"],
        {"telnyx": TELNYX_REF, APPROVAL_KEY: stale},
        org["id"],
        status="approved",
    )
    install(VERIFIED)

    r = await _refresh(client, token, org, tfv["id"])
    assert r.status_code == 200, r.text

    snap = await _snapshot(tfv["id"], org["id"])
    assert snap.status == "approved"  # refresh never moves the (terminal) local status
    ev = snap.carrier_refs[APPROVAL_KEY]
    assert ev["state"] == telnyx_approval.STATE_APPROVED
    assert ev["source"] == telnyx_approval.SOURCE_REFRESH
    assert _checked_at(ev) > datetime(2020, 1, 1, tzinfo=timezone.utc)


async def test_non_verified_refresh_revokes_evidence(telnyx_app, session):
    client, _app, install, _calls = telnyx_app
    token, org = await _register_and_org(client, "tfv-refresh-revoke@example.com")
    tfv = await _create_tollfree(client, token, org)
    await _seed(session, tfv["id"], {"telnyx": TELNYX_REF}, org["id"], status="approved")
    install({"id": TELNYX_REF, "verificationStatus": "Pending"})

    r = await _refresh(client, token, org, tfv["id"])
    assert r.status_code == 200, r.text

    snap = await _snapshot(tfv["id"], org["id"])
    assert snap.status == "approved"
    ev = snap.carrier_refs[APPROVAL_KEY]
    assert ev["state"] == telnyx_approval.STATE_REVOKED
    assert ev["source"] == telnyx_approval.SOURCE_REFRESH


async def test_refresh_failures_conflict_and_leave_refs_untouched(telnyx_app, session):
    client, _app, install, _calls = telnyx_app
    token, org = await _register_and_org(client, "tfv-refresh-fail@example.com")
    tfv = await _create_tollfree(client, token, org)
    await _seed(session, tfv["id"], {"telnyx": TELNYX_REF}, org["id"], status="approved")

    install(error=httpx.ConnectError("carrier unreachable"))
    assert (await _refresh(client, token, org, tfv["id"])).status_code == 409

    install({"id": OTHER_REF, "verificationStatus": "Verified"})
    assert (await _refresh(client, token, org, tfv["id"])).status_code == 409

    snap = await _snapshot(tfv["id"], org["id"])
    assert snap.status == "approved"
    assert snap.carrier_refs == {"telnyx": TELNYX_REF}


# ----------------------------------------------------------------------------------
# Guarding and carrier method
# ----------------------------------------------------------------------------------
async def test_refresh_is_operator_and_compliance_guarded(telnyx_app, session):
    client, _app, install, _calls = telnyx_app
    token, org = await _register_and_org(client, "tfv-guard@example.com")
    tfv = await _create_tollfree(client, token, org)
    await _seed(session, tfv["id"], {"telnyx": TELNYX_REF}, org["id"])
    install(VERIFIED)
    url = f"/api/v1/registration/tollfree/{tfv['id']}/refresh-telnyx"

    # Authenticated owner (holds compliance:manage) but WITHOUT the operator token.
    r = await client.post(
        url,
        headers={"Authorization": f"Bearer {token}", "X-Org-Id": str(org["id"])},
    )
    assert r.status_code in (401, 403), r.text

    # Operator token but no authentication/compliance context at all.
    r = await client.post(url, headers={OPS_HEADER: TEST_PLATFORM_OPS_TOKEN})
    assert r.status_code in (401, 403), r.text

    assert APPROVAL_KEY not in (await _snapshot(tfv["id"], org["id"])).carrier_refs


async def test_carrier_calls_are_get_only(telnyx_app, session):
    client, _app, install, calls = telnyx_app
    token, org = await _register_and_org(client, "tfv-getonly@example.com")
    tfv = await _create_tollfree(client, token, org)
    await _seed(session, tfv["id"], {"telnyx": TELNYX_REF}, org["id"])
    install(VERIFIED)

    assert (await _approve(client, token, org, tfv["id"])).status_code == 200
    assert (await _refresh(client, token, org, tfv["id"])).status_code == 200

    assert calls, "no carrier call was recorded"
    assert {request.method for request in calls} == {"GET"}
