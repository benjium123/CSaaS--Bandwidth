"""Ops view of self-serve 10DLC registrations: carrier ids, last event, numbers, timeline."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.db.base import set_org_context
from app.models import OrgNumber, TenDlcEvent, TenDlcRegistration
from tests.conftest import auth_headers, register_and_login
from tests.test_ops_console import _operator, ops, ops_settings  # noqa: F401
from tests.test_tendlc_self_serve import _workspace

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


async def _registration(session, *, stage="campaign_filed", rejection=None, brand_reused=False):
    org, brand, campaign, number = await _workspace(session)
    set_org_context(session, org.id)
    brand.carrier_refs = {"telnyx": "B-TEL-1"}
    campaign.carrier_refs = {"telnyx": "C-TEL-1"}
    number.campaign_id = campaign.id
    session.add(
        OrgNumber(
            org_id=org.id,
            e164=f"+1415555{uuid.uuid4().int % 10_000:04d}",
            carrier="telnyx",
            number_type="local",
            status="active",
            is_active=True,
            provisioning={},
        )
    )
    reg = TenDlcRegistration(
        org_id=org.id,
        brand_id=brand.id,
        campaign_id=campaign.id,
        fee_tier="standard",
        stage=stage,
        filing={"brand_reused": True} if brand_reused else {},
        rejection=rejection,
    )
    session.add(reg)
    await session.commit()
    return org, reg


def _event(org, reg, minutes, *, subject="campaign", telnyx_id="C-TEL-1", status="PENDING", **kw):
    return TenDlcEvent(
        org_id=org.id,
        registration_id=reg.id,
        subject=subject,
        telnyx_id=telnyx_id,
        event_type=kw.pop("event_type", "TCR_CAMPAIGN_UPDATE"),
        status=status,
        reasons=kw.pop("reasons", []),
        verified=kw.pop("verified", True),
        raw={"secret": "x"},
        received_at=NOW + timedelta(minutes=minutes),
        **kw,
    )


async def test_list_carries_ids_last_event_numbers_and_rejection(ops, session, ops_settings):
    token = await _operator(ops, session)
    org, reg = await _registration(
        session,
        rejection={"subject": "campaign", "reasons": ["bad opt-in"], "verified": True, "at": "x"},
        brand_reused=True,
    )
    set_org_context(session, org.id)
    session.add_all(
        [
            _event(org, reg, 1, status="PENDING"),
            _event(org, reg, 5, status="REJECTED", reasons=["bad opt-in"]),
            _event(org, reg, 3, subject="number", telnyx_id="+15125550999", status="FAILED"),
            _event(
                org,
                reg,
                4,
                subject="number",
                telnyx_id="+15125550999",
                status="FAILED",
                event_type="MNO_PROVISIONING_FAILED",
            ),
            _event(org, reg, 2, subject="number", telnyx_id="+15125550888", status="PROVISIONED"),
        ]
    )
    await session.commit()

    r = await ops.get("/api/v1/ops/texting-registrations", headers=auth_headers(token))
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["id"] == str(reg.id))
    assert row["brand_telnyx_id"] == "B-TEL-1"
    assert row["campaign_telnyx_id"] == "C-TEL-1"
    assert row["brand_reused"] is True
    assert row["rejection"]["reasons"] == ["bad opt-in"]
    assert row["last_event"]["status"] == "REJECTED"
    assert row["last_event"]["verified"] is True
    assert "raw" not in row["last_event"]
    assert row["numbers_assigned"] == 1
    assert row["numbers_failed"] == 1


async def test_row_without_events_has_null_last_event(ops, session, ops_settings):
    token = await _operator(ops, session)
    _, reg = await _registration(session)
    r = await ops.get("/api/v1/ops/texting-registrations", headers=auth_headers(token))
    row = next(x for x in r.json() if x["id"] == str(reg.id))
    assert row["last_event"] is None
    assert row["rejection"] is None
    assert row["brand_reused"] is False
    assert row["numbers_failed"] == 0


async def test_stage_filter(ops, session, ops_settings):
    token = await _operator(ops, session)
    _, a = await _registration(session, stage="active")
    _, b = await _registration(session, stage="needs_attention")
    r = await ops.get(
        "/api/v1/ops/texting-registrations?stage=needs_attention", headers=auth_headers(token)
    )
    ids = [x["id"] for x in r.json()]
    assert str(b.id) in ids and str(a.id) not in ids


async def test_events_endpoint_newest_first_and_404(ops, session, ops_settings):
    token = await _operator(ops, session)
    org, reg = await _registration(session)
    set_org_context(session, org.id)
    session.add_all([_event(org, reg, 1, status="OLD"), _event(org, reg, 9, status="NEW")])
    await session.commit()

    r = await ops.get(
        f"/api/v1/ops/texting-registrations/{reg.id}/events", headers=auth_headers(token)
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["status"] for i in items] == ["NEW", "OLD"]
    assert set(items[0]) == {
        "id", "subject", "telnyx_id", "event_type", "payload_type",
        "status", "reasons", "verified", "received_at",
    }  # fmt: skip

    r = await ops.get(
        f"/api/v1/ops/texting-registrations/{uuid.uuid4()}/events", headers=auth_headers(token)
    )
    assert r.status_code == 404


async def test_non_operator_is_refused(ops, session, ops_settings):
    token = await register_and_login(ops, "plain@example.com")
    _, reg = await _registration(session)
    for path in (
        "/api/v1/ops/texting-registrations",
        f"/api/v1/ops/texting-registrations/{reg.id}/events",
    ):
        r = await ops.get(path, headers=auth_headers(token))
        assert r.status_code == 403, r.text
