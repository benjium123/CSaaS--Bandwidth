"""10DLC phase 1: the carrier's timeline and its decline reasons are stored."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Org, TenDlcEvent


async def _org(session) -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Acme", slug=f"acme-{org_id.hex[:6]}"))
    await session.commit()
    set_org_context(session, org_id)
    return org_id


async def test_an_event_row_round_trips(session):
    org_id = await _org(session)
    session.add(
        TenDlcEvent(
            id=uuid.uuid4(),
            org_id=org_id,
            registration_id=None,
            subject="campaign",
            telnyx_id="C456",
            telnyx_event_id="evt-1",
            event_type="10dlc.campaign.update",
            payload_type="MNO_REVIEW",
            status="REJECTED",
            reasons=["Opt-in description is missing"],
            verified=False,
            raw={"data": {}},
            received_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    row = (await session.execute(sa.select(TenDlcEvent))).scalar_one()
    assert row.reasons == ["Opt-in description is missing"] and row.verified is False


from app.services import tendlc_events  # noqa: E402


def _body(event_type, payload, event_id="evt-1"):
    return {"data": {"id": event_id, "event_type": event_type, "payload": payload}}


def test_parse_campaign_rejection_with_reasons():
    ev = tendlc_events.parse(
        _body(
            "10dlc.campaign.update",
            {
                "campaignId": "C456",
                "type": "MNO_REVIEW",
                "status": "REJECTED",
                "reasons": [{"fields": ["messageFlow"], "description": "Opt-in is not described"}],
            },
        )
    )
    assert (ev.subject, ev.telnyx_id, ev.payload_type, ev.status) == (
        "campaign",
        "C456",
        "MNO_REVIEW",
        "REJECTED",
    )
    assert ev.reasons == ["Opt-in is not described"]
    assert tendlc_events.is_rejection(ev)


def test_parse_mno_description_only_and_brand_and_number():
    ev = tendlc_events.parse(
        _body(
            "10dlc.campaign.update",
            {
                "campaignId": "C456",
                "type": "MNO_REVIEW",
                "status": "REJECTED",
                "description": "Sample messages do not match the use case",
            },
        )
    )
    assert ev.reasons == ["Sample messages do not match the use case"]
    brand = tendlc_events.parse(
        _body(
            "10dlc.brand.update",
            {"brandId": "B123", "type": "REGISTRATION", "status": "REGISTRATION_FAILED"},
        )
    )
    assert (brand.subject, brand.telnyx_id) == ("brand", "B123")
    assert tendlc_events.is_rejection(brand)
    num = tendlc_events.parse(
        _body(
            "10dlc.phone_number.update",
            {
                "phoneNumber": "+15125550133",
                "type": "ASSIGNMENT",
                "status": "FAILED",
                "reasons": [{"description": "Number already assigned"}],
            },
        )
    )
    assert (num.subject, num.telnyx_id, num.reasons) == (
        "number",
        "+15125550133",
        ["Number already assigned"],
    )


def test_parse_refuses_a_body_with_no_id():
    no_id = {"data": {"event_type": "10dlc.campaign.update", "payload": {}}}
    assert tendlc_events.parse(no_id) is None
    assert tendlc_events.parse({"nonsense": True}) is None


async def test_record_dedupes_telnyx_retries(session):
    org_id = await _org(session)
    ev = tendlc_events.parse(
        _body(
            "10dlc.campaign.update",
            {"campaignId": "C456", "type": "TELNYX_REVIEW", "status": "ACCEPTED"},
        )
    )
    first = await tendlc_events.record(
        session, org_id=org_id, registration_id=None, event=ev, verified=True
    )
    await session.commit()
    again = await tendlc_events.record(
        session, org_id=org_id, registration_id=None, event=ev, verified=True
    )
    assert first is not None and again is None
    assert len((await session.execute(sa.select(TenDlcEvent))).scalars().all()) == 1


def test_rejection_text_falls_back():
    assert tendlc_events.rejection_text(None, "Generic.") == "Generic."
    assert tendlc_events.rejection_text({"reasons": []}, "Generic.") == "Generic."
    assert (
        tendlc_events.rejection_text(
            {"reasons": ["Opt-in is not described", "No privacy policy"]}, "Generic."
        )
        == "The carrier said: Opt-in is not described; No privacy policy."
    )
