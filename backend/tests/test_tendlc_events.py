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
