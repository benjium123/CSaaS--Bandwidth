"""Self-serve 10DLC: validate before taking money, then drive the real filing services.

Telnyx is an httpx.MockTransport that behaves like the carrier: a brand POST returns a
brandId, a GET reports whatever verdict the test sets. Stripe is a stub. Every carrier
write goes through the real filing services, so their one-attempt guards are exercised
rather than assumed.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.db.session import get_sessionmaker
from app.errors import ConflictError, ValidationFailedError
from app.models import Brand, Campaign, Org, OrgNumber, TenDlcRegistration
from app.services import stripe_client, tendlc
from app.services.telnyx_brand_filing import _ATTEMPT_KEY as BRAND_ATTEMPT_KEY
from tests.conftest import make_settings

ASSERTIONS = {
    "subscriberOptin": True,
    "subscriberOptout": True,
    "subscriberHelp": True,
    "numberPool": False,
    "directLending": False,
    "embeddedLink": False,
    "embeddedPhone": False,
    "ageGated": False,
    "autoRenewal": False,
    "termsAndConditions": True,
}


def _settings():
    return make_settings(
        telnyx_enabled=True,
        telnyx_api_key="telnyx-test-key",
        telnyx_public_key="telnyx-test-public",
        stripe_webhook_secret="whsec_test",
        public_web_url="https://ringlite.test",
    )


@pytest.fixture
def stripe_stub(monkeypatch):
    stripe = SimpleNamespace(
        checkout=SimpleNamespace(
            Session=SimpleNamespace(
                create=Mock(return_value={"id": "cs_tendlc", "url": "https://pay.test/cs"}),
                retrieve=Mock(return_value={"status": "open"}),
                expire=Mock(),
            )
        ),
        Subscription=SimpleNamespace(
            retrieve=Mock(return_value={"status": "trialing", "latest_invoice": "in_1"}),
            cancel=Mock(),
        ),
        InvoicePayment=SimpleNamespace(
            list=Mock(return_value={"data": [{"payment": {"payment_intent": "pi_1"}}]})
        ),
        Refund=SimpleNamespace(create=Mock(return_value={"id": "re_1"})),
    )
    monkeypatch.setattr(stripe_client, "_stripe", lambda settings: stripe)
    return stripe


class FakeTelnyx:
    """The carrier. Records every request; verdicts are set by the test."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.brand_status = {"status": "OK", "identityStatus": "UNVERIFIED"}
        self.campaign_status = {"campaignStatus": "TCR_PENDING"}
        self.fail_brand_post = False
        self.bad_pin = False

    def posts(self, suffix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST" and r.url.path.endswith(suffix)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/smsOtp"):
            if request.method == "PUT" and self.bad_pin:
                return httpx.Response(400, json={"errors": [{"detail": "Invalid OTP"}]})
            if request.method == "PUT":
                self.brand_status = {"status": "OK", "identityStatus": "VERIFIED"}
            return httpx.Response(200, json={"brandId": "B123", "referenceId": "OTP1"})
        if path.endswith("/10dlc/brand") and request.method == "POST":
            if self.fail_brand_post:
                return httpx.Response(503, json={"errors": [{"detail": "try later"}]})
            return httpx.Response(200, json={"brandId": "B123", **self.brand_status})
        if "/10dlc/brand/" in path:
            return httpx.Response(200, json={"brandId": "B123", **self.brand_status})
        if path.endswith("/10dlc/campaignBuilder"):
            return httpx.Response(200, json={"campaignId": "C456", "brandId": "B123"})
        if "/10dlc/campaign/" in path:
            return httpx.Response(200, json={"campaignId": "C456", **self.campaign_status})
        if path.endswith("/10dlc/phone_number_campaigns"):
            body = json.loads(request.content)
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={})


@pytest.fixture
async def telnyx():
    fake = FakeTelnyx()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as client:
        fake.client = client
        yield fake


async def _workspace(session, *, sole=False, use_case="CUSTOMER_CARE", vertical="PROFESSIONAL"):
    org = Org(id=uuid.uuid4(), name="Texting Co", slug=uuid.uuid4().hex)
    session.add(org)
    await session.commit()
    set_org_context(session, org.id)
    brand = Brand(
        org_id=org.id,
        name="Ada Solo" if sole else "Texting Co LLC",
        ein=None if sole else "12-3456789",
        entity_type="SOLE_PROPRIETOR" if sole else "PRIVATE_PROFIT",
        vertical=vertical,
        website="https://texting.test",
        email="owner@texting.test",
        phone="+12125550100",
        street="1 Main St",
        city="Austin",
        state="TX",
        postal_code="78701",
        country="US",
    )
    session.add(brand)
    await session.flush()
    campaign = Campaign(
        org_id=org.id,
        brand_id=brand.id,
        name="Customer texts",
        use_case=use_case,
        description="Appointment reminders and replies to customer questions for our clients.",
        opt_in_process="Customers opt in on our website booking form by ticking a box.",
        sample_messages=["Hi, your appointment is tomorrow at 3pm. Reply STOP to opt out."],
        help_message="Reply HELP for help.",
        opt_out_message="You are unsubscribed.",
    )
    number = OrgNumber(
        org_id=org.id,
        e164=f"+1512555{uuid.uuid4().int % 10_000:04d}",
        carrier="telnyx",
        number_type="local",
        status="active",
        is_active=True,
        provisioning={},
    )
    session.add_all([campaign, number])
    await session.commit()
    return org, brand, campaign, number


async def _checkout(session, org, brand, campaign, **overrides):
    kwargs = {
        "brand_id": brand.id,
        "campaign_id": campaign.id,
        "first_name": "Ada",
        "last_name": "Solo",
        "mobile_phone": None,
        "assertions": ASSERTIONS,
        "sub_usecases": [],
    } | overrides
    return await tendlc.start_checkout(session, _settings(), org.id, **kwargs)


async def _pay(session, reg):
    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": reg.checkout_id,
                "payment_status": "paid",
                "subscription": "sub_tendlc",
                "metadata": {"kind": "tendlc_fee", "registration_id": str(reg.id)},
            }
        },
    }
    assert await tendlc.handle_event(session, event) is True


async def _stage(reg_id) -> str:
    async with get_sessionmaker()() as s:
        from app.db.base import ALLOW_UNSCOPED_KEY

        return (
            await s.execute(
                sa.select(TenDlcRegistration.stage)
                .where(TenDlcRegistration.id == reg_id)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one()


# --------------------------------------------------------------------------------------
# Checkout: nobody pays for a registration the carrier would refuse
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("setup", "overrides", "message"),
    [
        ({"vertical": "Plumbing"}, {}, "industry"),
        ({"use_case": "MIXED"}, {"sub_usecases": ["MARKETING"]}, "sub_usecases"),
        ({"use_case": "SWEEPSTAKE"}, {}, "standard texting use cases"),
        ({"sole": True}, {"mobile_phone": "5551234"}, "mobile"),
        ({}, {"assertions": {**ASSERTIONS, "termsAndConditions": False}}, "termsAndConditions"),
    ],
)
async def test_checkout_refuses_what_the_carrier_would_refuse(
    session, stripe_stub, setup, overrides, message
):
    org, brand, campaign, _ = await _workspace(session, **setup)
    with pytest.raises(ValidationFailedError, match=message):
        await _checkout(session, org, brand, campaign, **overrides)
    stripe_stub.checkout.Session.create.assert_not_called()


async def test_checkout_charges_fees_plus_three_months_and_defers_the_monthly(session, stripe_stub):
    org, brand, campaign, _ = await _workspace(session, use_case="MIXED")
    reg = await _checkout(
        session, org, brand, campaign, sub_usecases=["CUSTOMER_CARE", "MARKETING"]
    )
    params = stripe_stub.checkout.Session.create.call_args.kwargs
    assert params["mode"] == "subscription"
    recurring, one_time = params["line_items"]
    assert recurring == {"price": _settings().stripe_tendlc_standard_price_id, "quantity": 1}
    # $4.50 brand + $15 review + 3 x $10 = $49.50 today, then $10/month from month four.
    assert one_time["price_data"]["unit_amount"] == 4950
    assert params["subscription_data"]["trial_period_days"] == 90
    assert reg.stage == "checkout"
    assert reg.filing["sub_usecases"] == ["CUSTOMER_CARE", "MARKETING"]


async def test_sole_proprietor_pays_the_two_dollar_tier(session, stripe_stub):
    org, brand, campaign, _ = await _workspace(session, sole=True)
    reg = await _checkout(
        session, org, brand, campaign, mobile_phone="+15125550199", sub_usecases=["CUSTOMER_CARE"]
    )
    params = stripe_stub.checkout.Session.create.call_args.kwargs
    assert params["line_items"][0]["price"] == _settings().stripe_tendlc_sole_prop_price_id
    assert params["line_items"][1]["price_data"]["unit_amount"] == 450 + 1500 + 3 * 200
    assert reg.fee_tier == "sole_proprietor"
    assert campaign.use_case == "SOLE_PROPRIETOR"


async def test_a_second_registration_waits_for_the_first(session, stripe_stub):
    org, brand, campaign, _ = await _workspace(session)
    reg = await _checkout(session, org, brand, campaign)
    await _pay(session, reg)
    with pytest.raises(ConflictError):
        await _checkout(session, org, brand, campaign)


# --------------------------------------------------------------------------------------
# The background job
# --------------------------------------------------------------------------------------
async def test_paid_registration_goes_all_the_way_to_a_texting_number(session, stripe_stub, telnyx):
    org, brand, campaign, number = await _workspace(session)
    reg = await _checkout(session, org, brand, campaign)
    reg_id = reg.id
    await _pay(session, reg)
    sm = get_sessionmaker()

    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg_id) == "brand_filed"
    assert len(telnyx.posts("/10dlc/brand")) == 1

    telnyx.brand_status = {"status": "OK", "identityStatus": "VERIFIED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg_id) == "campaign_filed"  # brand approved, campaign filed same pass
    filed = json.loads(telnyx.posts("/10dlc/campaignBuilder")[0].content)
    assert filed["usecase"] == "CUSTOMER_CARE" and filed["termsAndConditions"] is True

    telnyx.campaign_status = {"campaignStatus": "MNO_PROVISIONED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg_id) == "active"
    assigned = [json.loads(r.content) for r in telnyx.posts("/10dlc/phone_number_campaigns")]
    assert assigned == [{"phoneNumber": number.e164, "campaignId": "C456"}]

    async with sm() as s:
        set_org_context(s, org.id)
        campaign = await s.get(Campaign, campaign.id)
        number = await s.get(OrgNumber, number.id)
        assert campaign.status == "approved"
        assert campaign.carrier_refs["telnyx_approval"]["state"] == "approved"
        assert number.campaign_id == campaign.id

    # Nothing is filed twice on later passes.
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert len(telnyx.posts("/10dlc/brand")) == 1
    assert len(telnyx.posts("/10dlc/campaignBuilder")) == 1


async def test_an_ambiguous_brand_filing_stops_for_the_team_and_is_never_resent(
    session, stripe_stub, telnyx
):
    org, brand, campaign, _ = await _workspace(session)
    reg = await _checkout(session, org, brand, campaign)
    reg_id = reg.id
    await _pay(session, reg)
    telnyx.fail_brand_post = True
    sm = get_sessionmaker()

    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg_id) == "needs_attention"
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert len(telnyx.posts("/10dlc/brand")) == 1
    async with sm() as s:
        set_org_context(s, org.id)
        assert (await s.get(Brand, brand.id)).carrier_refs.get(BRAND_ATTEMPT_KEY)


async def test_rejected_business_is_refunded_everything_but_the_brand_fee(
    session, stripe_stub, telnyx
):
    org, brand, campaign, _ = await _workspace(session)
    reg = await _checkout(session, org, brand, campaign)
    reg_id = reg.id
    await _pay(session, reg)
    sm = get_sessionmaker()
    await tendlc.tick(sm, _settings(), client=telnyx.client)

    telnyx.brand_status = {"status": "REGISTRATION_FAILED", "identityStatus": "UNVERIFIED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)

    assert await _stage(reg_id) == "brand_rejected"
    refund = stripe_stub.Refund.create.call_args.kwargs
    assert (refund["payment_intent"], refund["amount"]) == ("pi_1", 1500 + 3 * 1000)
    stripe_stub.Subscription.cancel.assert_called_once()
    assert telnyx.posts("/10dlc/campaignBuilder") == []


async def test_sole_proprietor_is_texted_a_pin_and_verifies_it(session, stripe_stub, telnyx):
    org, brand, campaign, _ = await _workspace(session, sole=True)
    reg = await _checkout(
        session, org, brand, campaign, mobile_phone="+15125550199", sub_usecases=["CUSTOMER_CARE"]
    )
    reg_id = reg.id
    await _pay(session, reg)
    sm = get_sessionmaker()

    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg_id) == "otp_pending"
    filed = json.loads(telnyx.posts("/10dlc/brand")[0].content)
    assert filed["entityType"] == "SOLE_PROPRIETOR"
    assert filed["mobilePhone"] == "+15125550199"
    pin_request = json.loads(telnyx.posts("/smsOtp")[0].content)
    assert "@OTP_PIN@" in pin_request["pinSms"]

    async with sm() as s:
        set_org_context(s, org.id)
        live = await s.get(TenDlcRegistration, reg_id)
        telnyx.bad_pin = True
        with pytest.raises(ValidationFailedError, match="did not match"):
            await tendlc.verify_otp(s, _settings(), live, "123456", client=telnyx.client)
        assert live.stage == "otp_pending"

        telnyx.bad_pin = False
        await tendlc.verify_otp(s, _settings(), live, "654321", client=telnyx.client)
    assert await _stage(reg_id) == "campaign_filed"
    filed_campaign = json.loads(telnyx.posts("/10dlc/campaignBuilder")[0].content)
    assert filed_campaign["usecase"] == "SOLE_PROPRIETOR"
    assert filed_campaign["subUsecases"] == ["CUSTOMER_CARE"]


async def test_new_numbers_join_an_active_campaign_and_sole_proprietors_keep_one(
    session, stripe_stub, telnyx
):
    org, brand, campaign, first = await _workspace(session, sole=True)
    # Two numbers already exist when the campaign is approved: only one may join it.
    session.add(
        OrgNumber(
            org_id=org.id,
            e164="+15125550666",
            carrier="telnyx",
            number_type="local",
            status="active",
            is_active=True,
            provisioning={},
        )
    )
    await session.commit()
    reg = await _checkout(
        session, org, brand, campaign, mobile_phone="+15125550199", sub_usecases=["CUSTOMER_CARE"]
    )
    await _pay(session, reg)
    sm = get_sessionmaker()
    # The carrier verifies a sole proprietor only once the PIN is entered (the fake flips
    # identityStatus on the PUT), exactly as Telnyx does.
    telnyx.campaign_status = {"campaignStatus": "MNO_PROVISIONED"}
    await tendlc.tick(sm, _settings(), client=telnyx.client)  # file brand + send PIN
    async with sm() as s:
        set_org_context(s, org.id)
        live = await s.get(TenDlcRegistration, reg.id)
        await tendlc.verify_otp(s, _settings(), live, "654321", client=telnyx.client)
    await tendlc.tick(sm, _settings(), client=telnyx.client)
    assert await _stage(reg.id) == "active"

    set_org_context(session, org.id)
    session.add(
        OrgNumber(
            org_id=org.id,
            e164="+15125550777",
            carrier="telnyx",
            number_type="local",
            status="active",
            is_active=True,
            provisioning={},
        )
    )
    await session.commit()
    await tendlc.tick(sm, _settings(), client=telnyx.client)

    assigned = [
        json.loads(r.content)["phoneNumber"] for r in telnyx.posts("/phone_number_campaigns")
    ]
    assert assigned == [first.e164]  # a sole proprietor campaign takes one number only


async def test_stripe_events_for_other_products_are_not_ours(session):
    event = {"type": "checkout.session.completed", "data": {"object": {"metadata": {}}}}
    assert await tendlc.handle_event(session, event) is False
