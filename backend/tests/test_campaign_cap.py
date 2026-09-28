"""A 10DLC campaign holds at most 50 numbers (Telnyx's limit), enforced server-side.

The 51st number is refused with a 409 and a plain message; a number already on the
campaign never counts twice; moving a number to another campaign frees its slot.
"""

from __future__ import annotations

import uuid

from app.db.session import get_sessionmaker
from app.models import OrgNumber
from app.services import registration as reg
from tests.test_numbers_registration import LOCAL2, _approved_campaign, _org


async def _fill(org_id: str, campaign_id: str, count: int) -> None:
    """Put ``count`` extra numbers straight onto the campaign."""
    async with get_sessionmaker()() as session:
        session.info["org_id"] = uuid.UUID(org_id)
        for i in range(count):
            session.add(
                OrgNumber(
                    id=uuid.uuid4(),
                    org_id=uuid.UUID(org_id),
                    e164=f"+1469555{i:04d}",
                    carrier="bandwidth",
                    is_active=True,
                    status="active",
                    campaign_id=uuid.UUID(campaign_id),
                )
            )
        await session.commit()


async def _assign(client, h, number_id: str, campaign_id: str):
    return await client.patch(
        f"/api/v1/numbers/{number_id}/campaign", json={"campaign_id": campaign_id}, headers=h
    )


async def _full_campaign(client, email: str):
    """An org whose fixture number is the 50th number on campaign A."""
    _token, org, h, number = await _org(client, email)
    campaign = await _approved_campaign(client, h)
    await _fill(org["id"], campaign["id"], reg.MAX_NUMBERS_PER_CAMPAIGN - 1)
    r = await _assign(client, h, number["id"], campaign["id"])
    assert r.status_code == 200, r.text
    second = await client.post("/api/v1/numbers", json={"e164": LOCAL2}, headers=h)
    assert second.status_code == 201, second.text
    return h, number, campaign, second.json()


async def test_fifty_fit_and_the_fifty_first_is_refused(app_with_carrier):
    client, _fake, _ = app_with_carrier
    h, _number, campaign, second = await _full_campaign(client, "cap1@example.com")

    r = await _assign(client, h, second["id"], campaign["id"])
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "campaign_full"
    assert r.json()["error"]["message"] == (
        "This campaign is full (50 numbers). Attach another campaign."
    )


async def test_reassigning_a_number_already_on_the_campaign_does_not_count_twice(
    app_with_carrier,
):
    client, _fake, _ = app_with_carrier
    h, number, campaign, _second = await _full_campaign(client, "cap2@example.com")

    r = await _assign(client, h, number["id"], campaign["id"])
    assert r.status_code == 200, r.text
    assert r.json()["campaign_id"] == campaign["id"]


async def test_moving_a_number_to_another_campaign_frees_its_slot(app_with_carrier):
    client, _fake, _ = app_with_carrier
    h, number, campaign, second = await _full_campaign(client, "cap3@example.com")
    other = await _approved_campaign(client, h, name="Camp B")

    moved = await _assign(client, h, number["id"], other["id"])
    assert moved.status_code == 200, moved.text

    r = await _assign(client, h, second["id"], campaign["id"])
    assert r.status_code == 200, r.text


async def test_campaign_list_reports_used_and_cap(app_with_carrier):
    client, _fake, _ = app_with_carrier
    h, _number, campaign, _second = await _full_campaign(client, "cap4@example.com")

    r = await client.get("/api/v1/registration/campaigns", headers=h)
    assert r.status_code == 200, r.text
    row = next(c for c in r.json() if c["id"] == campaign["id"])
    assert row["number_count"] == 50
    assert row["number_cap"] == 50
