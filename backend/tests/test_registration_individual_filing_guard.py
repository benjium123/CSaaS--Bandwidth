"""P41 individual-workspace guard on the three BILLABLE 10DLC / toll-free filing routes.

`_require_messaging_enabled` (registration.py) refuses every messaging mutation for an
individual (`account_type == "individual"`) workspace. The local `/submit` and the
`/status` routes are already covered by ``test_individual_telephony``; this file closes the
remaining gap on the three operator-only, billable ``/file-telnyx`` routes - the only place
a carrier is actually contacted and money is actually spent.

Each filing is refused with the EXISTING ``individual_messaging_disabled`` denial BEFORE
the carrier client is reached. The live Telnyx client is replaced with an injected
``httpx.MockTransport`` (the same seam ``registration._injected_http_client`` reads) that
records - and refuses - any request, so the guard is proven to sit in front of the carrier
rather than behind it. No secret is configured and no network is touched: the suite's
``platform_ops_token`` is the only credential the requests carry.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.models import Org
from tests.conftest import TEST_PLATFORM_OPS_TOKEN

PASSWORD = "correct-horse-battery"
INDIVIDUAL_SMS_CODE = "individual_messaging_disabled"

BRAND_FILE = "/api/v1/registration/brands/{brand_id}/file-telnyx"
CAMPAIGN_FILE = "/api/v1/registration/campaigns/{campaign_id}/file-telnyx"
TFV_FILE = "/api/v1/registration/tollfree/{tfv_id}/file-telnyx"

#: A valid operator filing body per endpoint. The bodies only have to satisfy request
#: validation: the individual guard runs before the record is looked up, so a placeholder
#: path id is enough - the request never gets far enough to need the record.
BRAND_BODY = {
    "confirm_non_refundable": True,
    "company_name": "Individual Workspace LLC",
    "first_name": "Dana",
    "last_name": "Reyes",
    "brand_relationship": "DIRECT",
}
CAMPAIGN_BODY = {
    "confirm_non_refundable": True,
    "assertions": {"autoRenewal": True, "termsAndConditions": True},
}
#: The complete, valid non-sole-proprietor Telnyx toll-free body: every documented
#: camelCase field is required and ``FileTfvTelnyxIn`` forbids extras.
TFV_BODY = {
    "confirm_non_refundable": True,
    "sole_proprietor": False,
    "businessName": "Sabine Property Group LLC",
    "corporateWebsite": "https://sabineproperty.example",
    "businessAddr1": "100 Main Street",
    "businessCity": "Austin",
    "businessState": "TX",
    "businessZip": "78701",
    "businessContactFirstName": "Dana",
    "businessContactLastName": "Reyes",
    "businessContactEmail": "dana@sabineproperty.example",
    "businessContactPhone": "+15125550123",
    "useCaseSummary": "Appointment notices to opted-in residents.",
    "productionMessageContent": "Sabine: your appointment is tomorrow. Reply STOP to opt out.",
    "optInWorkflow": "Residents opt in via a checkbox on the tenant portal.",
    "additionalInformation": "No additional information.",
    "useCase": "Appointments",
    "messageVolume": "10,000",
    "optInWorkflowImageURLs": ["https://sabineproperty.example/optin.png"],
    "businessRegistrationNumber": "87-1234567",
    "businessRegistrationType": "EIN",
    "businessRegistrationCountry": "US",
}

FILINGS = (
    pytest.param("brand", BRAND_FILE, BRAND_BODY, id="brand"),
    pytest.param("campaign", CAMPAIGN_FILE, CAMPAIGN_BODY, id="campaign"),
    pytest.param("tollfree", TFV_FILE, TFV_BODY, id="tollfree"),
)


class _CarrierSpy:
    """An ``httpx`` transport that records every request and refuses to answer one."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise AssertionError("the Telnyx carrier client must not be called")


@pytest.fixture
async def guarded(app_with_carrier):
    """The suite app with an injected Telnyx transport that records (and refuses) calls.

    ``app_with_carrier`` is the existing app fixture; ``state.telnyx_http_client`` is the
    injection seam ``registration._injected_http_client`` reads, so no live socket is ever
    opened by the filing routes under test.
    """
    client, _fake_carrier, application = app_with_carrier
    spy = _CarrierSpy()
    telnyx_client = httpx.AsyncClient(transport=httpx.MockTransport(spy.handler))
    application.state.telnyx_http_client = telnyx_client
    try:
        yield client, spy
    finally:
        await telnyx_client.aclose()


async def _new_org(session, client, email, *, account_type):
    """Register + create an org through the real API and pin its ``account_type``."""
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
        "/api/v1/orgs",
        json={"name": email},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    org_id = uuid.UUID(r.json()["id"])
    org = (await session.execute(sa.select(Org).where(Org.id == org_id))).scalar_one()
    org.account_type = account_type
    await session.commit()
    return org_id, token


def _headers(token, org_id):
    return {
        "Authorization": f"Bearer {token}",
        "X-Org-Id": str(org_id),
        "X-Platform-Ops-Token": TEST_PLATFORM_OPS_TOKEN,
    }


@pytest.mark.parametrize("label,path,body", FILINGS)
async def test_individual_filing_refused_before_the_carrier(
    guarded, session, label, path, body
):
    client, spy = guarded
    org_id, token = await _new_org(
        session, client, f"ind-file-{label}@example.com", account_type="individual"
    )
    url = path.format(
        brand_id=uuid.uuid4(), campaign_id=uuid.uuid4(), tfv_id=uuid.uuid4()
    )
    r = await client.post(url, json=body, headers=_headers(token, org_id))
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == INDIVIDUAL_SMS_CODE
    # The carrier client was never constructed or called: the guard refused first.
    assert spy.requests == []


async def test_business_filing_still_reaches_the_existing_lookup(guarded, session):
    """Control: a business workspace is NOT caught by the individual guard.

    The same brand filing reaches the pre-existing, unchanged brand lookup and its
    existing 404; a real brand would instead continue into ``file_brand_with_telnyx``,
    which the billable filing-route tests cover. Either way the individual denial is
    never returned to a business workspace and no carrier is reached.
    """
    client, spy = guarded
    org_id, token = await _new_org(
        session, client, "biz-file-control@example.com", account_type="business"
    )
    r = await client.post(
        BRAND_FILE.format(brand_id=uuid.uuid4()),
        json=BRAND_BODY,
        headers=_headers(token, org_id),
    )
    assert r.status_code == 404, r.text
    error = r.json().get("error", {})
    assert error.get("code") != INDIVIDUAL_SMS_CODE
    assert spy.requests == []
