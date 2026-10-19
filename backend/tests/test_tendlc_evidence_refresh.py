"""Daily 10DLC evidence refresh: once per Central day at 06:00, one read-only Telnyx GET per
approved campaign; approved -> fresh evidence, not approved -> revoked, failure -> untouched."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import httpx
import pytest

from app.compliance import telnyx_approval
from app.models.numbers import Brand, Campaign
from app.models.org import Org
from app.services import tendlc_evidence_refresh as refresh
from tests.conftest import make_settings

CAMPAIGN_REF = "40000000-0000-4000-8000-000000000001"
OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)
# 2026-10-20 11:30 UTC = 06:30 CDT
AFTER_SIX = datetime(2026, 10, 20, 11, 30, tzinfo=timezone.utc)


class _Stub:
    def __init__(self, payload: dict | None = None, fail: bool = False) -> None:
        self.payload = payload or {}
        self.fail = fail
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET", "refresh must never file"
        if self.fail:
            return httpx.Response(500, json={})
        return httpx.Response(200, json=self.payload)


@pytest.fixture(autouse=True)
def reset_day(monkeypatch):
    monkeypatch.setattr(refresh, "_last_run_day", None)


async def _seed(session, *, status: str = "approved", ref: str | None = CAMPAIGN_REF):
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Evidence Co", slug=f"ev-{org_id.hex[:12]}"))
    await session.flush()
    session.info["org_id"] = org_id
    brand = Brand(id=uuid.uuid4(), org_id=org_id, name="Evidence Brand")
    session.add(brand)
    await session.flush()
    refs = {}
    if ref:
        refs["telnyx"] = ref
        refs["telnyx_approval"] = telnyx_approval.build_evidence(
            state=telnyx_approval.STATE_APPROVED, carrier_id=ref, checked_at=OLD,
            source=telnyx_approval.SOURCE_STATUS_DECISION,
        )
    campaign = Campaign(
        id=uuid.uuid4(), org_id=org_id, brand_id=brand.id, name="Evidence Campaign",
        use_case="MIXED", sample_messages=[], status=status, carrier_refs=refs,
    )
    session.add(campaign)
    await session.commit()
    session.info.pop("org_id", None)
    return org_id, campaign.id


async def _evidence(session, org_id, campaign_id):
    session.info["org_id"] = org_id
    session.expire_all()
    row = await session.get(Campaign, campaign_id)
    session.info.pop("org_id", None)
    return telnyx_approval.parse_evidence(row.carrier_refs)


async def _run(session, stub, **kw):
    client = httpx.AsyncClient(transport=httpx.MockTransport(stub.handler))
    try:
        return await refresh.tick(
            session, make_settings(telnyx_api_key="test-key-not-real"),
            http_client=client, now=AFTER_SIX, **kw,
        )
    finally:
        await client.aclose()


def test_due_once_per_central_day_at_six():
    before_six = datetime(2026, 10, 20, 10, 59, tzinfo=timezone.utc)  # 05:59 CDT
    assert not refresh.is_due(before_six, None)
    assert refresh.is_due(AFTER_SIX, None)
    assert not refresh.is_due(AFTER_SIX, date(2026, 10, 20))
    assert refresh.is_due(AFTER_SIX, date(2026, 10, 19))


async def test_approved_campaign_gets_fresh_evidence(session):
    org_id, cid = await _seed(session)
    stub = _Stub({"campaignId": CAMPAIGN_REF, "campaignStatus": "MNO_PROVISIONED"})
    out = await _run(session, stub)
    assert out["tendlc_evidence_refreshed"] == 1
    assert len(stub.requests) == 1
    ev = await _evidence(session, org_id, cid)
    assert ev.state == telnyx_approval.STATE_APPROVED
    assert ev.source == telnyx_approval.SOURCE_REFRESH
    # Second tick the same day does nothing.
    assert await _run(session, stub) == {}
    assert len(stub.requests) == 1


async def test_not_approved_at_telnyx_revokes(session):
    org_id, cid = await _seed(session)
    stub = _Stub({"campaignId": CAMPAIGN_REF, "campaignStatus": "TCR_SUSPENDED"})
    out = await _run(session, stub)
    assert out["tendlc_evidence_revoked"] == 1
    ev = await _evidence(session, org_id, cid)
    assert ev.state == telnyx_approval.STATE_REVOKED


async def test_carrier_failure_leaves_evidence_untouched(session):
    org_id, cid = await _seed(session)
    out = await _run(session, _Stub(fail=True))
    assert out["tendlc_evidence_failed"] == 1
    ev = await _evidence(session, org_id, cid)
    assert ev.source == telnyx_approval.SOURCE_STATUS_DECISION


async def test_mismatched_campaign_is_refused(session):
    org_id, cid = await _seed(session)
    stub = _Stub({"campaignId": "some-other-campaign", "campaignStatus": "MNO_PROVISIONED"})
    out = await _run(session, stub)
    assert out["tendlc_evidence_failed"] == 1
    ev = await _evidence(session, org_id, cid)
    assert ev.source == telnyx_approval.SOURCE_STATUS_DECISION


async def test_draft_or_unfiled_campaigns_are_skipped(session):
    await _seed(session, status="draft")
    await _seed(session, ref=None)
    stub = _Stub({"campaignId": CAMPAIGN_REF, "campaignStatus": "MNO_PROVISIONED"})
    out = await _run(session, stub)
    assert out == {
        "tendlc_evidence_refreshed": 0, "tendlc_evidence_revoked": 0,
        "tendlc_evidence_failed": 0,
    }
    assert stub.requests == []
