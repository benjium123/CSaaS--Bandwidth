"""Offline success test for ``telnyx_number_association.associate_number_with_telnyx``.

Real service against the real test DB with ``httpx.MockTransport`` standing in for Telnyx:
one campaign GET the carrier reports approved (``MNO_PROVISIONED``) and then exactly one
``POST /10dlc/phone_number_campaigns``. Flat JSON, as the Telnyx 10DLC client expects.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.models.messaging import OrgNumber
from app.models.numbers import Brand, Campaign
from app.models.org import Org
from app.services import telnyx_number_association as svc
from tests.conftest import make_settings

pytestmark = pytest.mark.asyncio

CARRIER_CAMPAIGN_ID = "carrier-campaign-abc123"
E164 = "+12025550123"


def _mock_transport(seen: dict) -> httpx.MockTransport:
    """Answer the campaign read and the assignment, recording both requests."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            seen["campaign_get_url"] = str(request.url)
            body = {
                "campaignId": CARRIER_CAMPAIGN_ID,
                "campaignStatus": "MNO_PROVISIONED",
            }
        else:
            seen["assign_method"] = request.method
            seen["assign_path"] = request.url.path
            seen["assign_body"] = json.loads(request.content)
            body = {"phoneNumber": E164, "campaignId": CARRIER_CAMPAIGN_ID}
        # Flat payloads - the Telnyx 10DLC client does not unwrap a "data" envelope.
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


async def test_associate_number_with_telnyx_success(session):
    tx_settings = make_settings(telnyx_api_key="test-telnyx-key")

    org = Org(name="Number Association Org", slug="number-association-org")
    session.add(org)
    await session.flush()

    # Pin the tenant BEFORE any TenantScoped row is written, or the write guard refuses.
    session.info["org_id"] = org.id
    try:
        # Campaign.brand_id is NOT NULL; flush the brand so its uuid default is populated.
        brand = Brand(org_id=org.id, name="Approved brand")
        session.add(brand)
        await session.flush()

        campaign = Campaign(
            org_id=org.id,
            brand_id=brand.id,
            name="Approved 10DLC campaign",
            status="approved",
            carrier_refs={"telnyx": CARRIER_CAMPAIGN_ID},
        )
        number = OrgNumber(
            org_id=org.id,
            e164=E164,
            carrier="telnyx",
            number_type="local",
            status="active",
            is_active=True,
            provisioning={},
        )
        session.add_all([campaign, number])
        await session.commit()

        seen: dict = {}
        async with httpx.AsyncClient(transport=_mock_transport(seen)) as client:
            result = await svc.associate_number_with_telnyx(
                session, tx_settings, number, campaign, client=client
            )

        # Carrier saw the campaign lookup at the exact carrier id, then one assignment.
        assert CARRIER_CAMPAIGN_ID in seen["campaign_get_url"]
        assert seen["assign_method"] == "POST"
        assert seen["assign_path"].endswith("/10dlc/phone_number_campaigns")
        assert seen["assign_body"] == {
            "phoneNumber": E164,
            "campaignId": CARRIER_CAMPAIGN_ID,
        }

        # Persisted: campaign_id set and the marker says "assigned" with matching ids.
        await session.refresh(result)
        await session.refresh(number)
        assert result.campaign_id == campaign.id
        assert number.campaign_id == campaign.id
        marker = number.provisioning[svc.ATTEMPT_KEY]
        assert marker["state"] == svc.MARKER_STATE_ASSIGNED
        assert marker["carrier_outcome"] == "accepted"
        assert marker["campaign_id"] == str(campaign.id)
        assert marker["carrier_campaign_id"] == CARRIER_CAMPAIGN_ID
    finally:
        session.info.pop("org_id", None)
