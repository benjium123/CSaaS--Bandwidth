"""Offline failure tests for the Telnyx number->campaign association service.

Every case drives the real service against ``httpx.MockTransport`` - no live carrier.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.errors import ConflictError, FeatureUnavailableError, ValidationFailedError
from app.models.messaging import OrgNumber
from app.models.numbers import Brand, Campaign
from app.models.org import Org
from app.services import telnyx_number_association as svc
from tests.conftest import make_settings

pytestmark = pytest.mark.asyncio

CARRIER_CAMPAIGN_ID = "carrier-campaign-abc123"
E164 = "+12025550123"
WRONG_E164 = "+12025559999"
APPROVED = {"campaignId": CARRIER_CAMPAIGN_ID, "campaignStatus": "MNO_PROVISIONED"}
PENDING = {"campaignId": CARRIER_CAMPAIGN_ID, "campaignStatus": "PENDING"}
ECHOED = {"phoneNumber": E164, "campaignId": CARRIER_CAMPAIGN_ID}


async def _seed(session, *, provisioning=None):
    """Create a locally-approved org/campaign/number and pin the tenant; the caller must
    pop ``session.info["org_id"]`` in its own ``finally``."""
    settings = make_settings(telnyx_api_key="test-telnyx-key")
    org = Org(name="Failures Org", slug=f"assoc-fail-{uuid.uuid4().hex}")
    session.add(org)
    await session.flush()

    # Pin the tenant BEFORE any TenantScoped row is written, or the write guard refuses.
    session.info["org_id"] = org.id

    brand = Brand(org_id=org.id, name="Failure brand")
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
        provisioning=provisioning or {},
    )
    session.add_all([campaign, number])
    await session.commit()
    return settings, org, campaign, number


def _transport(get_body, post):
    """``post`` is a JSON body to echo, or a callable ``request -> Response``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=get_body)
        if callable(post):
            return post(request)
        return httpx.Response(200, json=post)

    return httpx.MockTransport(handler)


async def test_carrier_pending_campaign_spends_no_post(session):
    settings, _org, campaign, number = await _seed(session)
    posts: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200, json=ECHOED)

    try:
        async with httpx.AsyncClient(transport=_transport(PENDING, record)) as client:
            with pytest.raises(ValidationFailedError):
                await svc.associate_number_with_telnyx(
                    session, settings, number, campaign, client=client
                )

        assert posts == []
        await session.refresh(number)
        assert number.campaign_id is None
        assert svc.ATTEMPT_KEY not in (number.provisioning or {})
    finally:
        session.info.pop("org_id", None)


async def test_assignment_timeout_leaves_unconfirmed_marker(session):
    settings, _org, campaign, number = await _seed(session)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("assignment timed out", request=request)

    try:
        async with httpx.AsyncClient(transport=_transport(APPROVED, timeout)) as client:
            with pytest.raises((FeatureUnavailableError, httpx.HTTPError)):
                await svc.associate_number_with_telnyx(
                    session, settings, number, campaign, client=client
                )

        await session.refresh(number)
        assert number.campaign_id is None
        marker = number.provisioning[svc.ATTEMPT_KEY]
        assert marker["state"] == svc.MARKER_STATE_UNCONFIRMED
        assert marker["carrier_outcome"] == "unknown"
    finally:
        session.info.pop("org_id", None)


async def test_mismatched_echoed_phone_keeps_marker_unconfirmed(session):
    settings, _org, campaign, number = await _seed(session)
    echoed = {"phoneNumber": WRONG_E164, "campaignId": CARRIER_CAMPAIGN_ID}

    try:
        async with httpx.AsyncClient(transport=_transport(APPROVED, echoed)) as client:
            with pytest.raises(ValidationFailedError):
                await svc.associate_number_with_telnyx(
                    session, settings, number, campaign, client=client
                )

        await session.refresh(number)
        assert number.campaign_id is None
        marker = number.provisioning[svc.ATTEMPT_KEY]
        assert marker["state"] == svc.MARKER_STATE_UNCONFIRMED
    finally:
        session.info.pop("org_id", None)


async def test_existing_marker_refuses_without_carrier_call(session):
    settings, _org, campaign, number = await _seed(
        session,
        provisioning={
            svc.ATTEMPT_KEY: {
                "state": svc.MARKER_STATE_UNCONFIRMED,
                "carrier_outcome": "unknown",
            }
        },
    )
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    try:
        async with httpx.AsyncClient(transport=_transport(APPROVED, record)) as client:
            with pytest.raises(ConflictError):
                await svc.associate_number_with_telnyx(
                    session, settings, number, campaign, client=client
                )

        assert requests == []
        await session.refresh(number)
        assert number.campaign_id is None
    finally:
        session.info.pop("org_id", None)
