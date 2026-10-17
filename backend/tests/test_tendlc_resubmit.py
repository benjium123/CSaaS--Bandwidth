"""Campaign-only resubmit: a declined campaign under an already approved business pays the
campaign review + service fee + 3 months, never the brand fee again."""

from __future__ import annotations

import uuid

import pytest

from app.db.base import set_org_context
from app.db.session import get_sessionmaker
from app.errors import ConflictError
from app.models import Brand, Campaign, TenDlcRegistration
from app.services import tendlc
from tests.test_tendlc_self_serve import (  # noqa: F401
    _checkout,
    _pay,
    _settings,
    _stage,
    _workspace,
    stripe_stub,
    telnyx,
)


async def _reused(session, *, sole=False, campaign_status="rejected"):
    org, brand, campaign, number = await _workspace(session, sole=sole)
    brand.status = "approved"
    brand.carrier_refs = {"telnyx": "B123"}
    campaign.status = campaign_status
    campaign.carrier_refs = {
        "telnyx": "C_OLD",
        "telnyx_filing": {"state": "submitted", "reference_id": "r1"},
    }
    await session.commit()
    return org, brand, campaign, number


def test_quote_reused_drops_the_brand_fee():
    q = tendlc.quote("standard", brand_reused=True)
    assert q["brand_fee_cents"] == 0
    assert q["due_today_cents"] == 1800 + 500 + 3 * 1250 == 6050
    assert tendlc.quote("sole_proprietor", brand_reused=True)["due_today_cents"] == 3350
    assert tendlc.quote("standard")["due_today_cents"] == 6650  # unchanged
    assert tendlc.customer_quote("standard", brand_reused=True)["due_today_cents"] == 6050


async def test_reused_brand_checkout_charges_campaign_only(session, stripe_stub):
    org, brand, campaign, _ = await _reused(session)
    reg = await _checkout(session, org, brand, campaign)
    params = stripe_stub.checkout.Session.create.call_args.kwargs
    assert params["line_items"][1]["price_data"]["unit_amount"] == 6050
    assert reg.filing["brand_reused"] is True


async def test_reused_sole_proprietor_pays_3350_with_no_mobile(session, stripe_stub):
    org, brand, campaign, _ = await _reused(session, sole=True)
    await _checkout(
        session, org, brand, campaign, mobile_phone=None, sub_usecases=["CUSTOMER_CARE"]
    )
    params = stripe_stub.checkout.Session.create.call_args.kwargs
    assert params["line_items"][1]["price_data"]["unit_amount"] == 3350


async def test_registered_brand_that_is_not_approved_still_conflicts(session, stripe_stub):
    org, brand, campaign, _ = await _workspace(session)
    brand.status = "submitted"
    brand.carrier_refs = {"telnyx": "B123"}
    await session.commit()
    with pytest.raises(ConflictError):
        await _checkout(session, org, brand, campaign)


@pytest.mark.parametrize("status", ["submitted", "approved"])
async def test_reused_brand_still_refuses_a_live_campaign(session, stripe_stub, status):
    org, brand, campaign, _ = await _reused(session, campaign_status=status)
    with pytest.raises(ConflictError):
        await _checkout(session, org, brand, campaign)


async def test_rejected_campaign_without_a_reused_brand_conflicts(session, stripe_stub):
    org, brand, campaign, _ = await _workspace(session)
    campaign.status = "rejected"
    await session.commit()
    with pytest.raises(ConflictError):
        await _checkout(session, org, brand, campaign)


async def test_paid_reused_files_a_new_campaign_without_brand_or_otp(
    session, stripe_stub, telnyx
):
    org, brand, campaign, _ = await _reused(session, sole=True)
    reg = await _checkout(
        session, org, brand, campaign, mobile_phone="+15125550199", sub_usecases=["CUSTOMER_CARE"]
    )
    reg_id = reg.id
    await _pay(session, reg)
    telnyx.brand_status = {"status": "OK", "identityStatus": "VERIFIED"}
    sm = get_sessionmaker()
    await tendlc.tick(sm, _settings(), client=telnyx.client)

    assert await _stage(reg_id) == "campaign_filed"
    assert telnyx.posts("/10dlc/brand") == []
    assert telnyx.posts("/smsOtp") == []
    assert len(telnyx.posts("/10dlc/campaignBuilder")) == 1
    async with sm() as s:
        set_org_context(s, org.id)
        c = await s.get(Campaign, campaign.id)
        assert c.status == "submitted"
        assert c.carrier_refs["telnyx"] == "C456"
        assert c.carrier_refs["telnyx_previous"] == ["C_OLD"]
        b = await s.get(Brand, brand.id)
        assert b.carrier_refs == {"telnyx": "B123"}


async def test_reused_cancel_at_paid_refunds_review_and_months_only(session, stripe_stub):
    org, brand, campaign, _ = await _reused(session)
    reg = await _checkout(session, org, brand, campaign)
    await _pay(session, reg)
    async with get_sessionmaker()() as s:
        await tendlc.cancel(s, _settings(), reg.id)
    assert stripe_stub.Refund.create.call_args.kwargs["amount"] == 1800 + 3 * 1250


async def test_reused_cancel_after_archive_refunds_review_and_months_only(session, stripe_stub):
    org, brand, campaign, _ = await _reused(session)
    reg = await _checkout(session, org, brand, campaign)
    await _pay(session, reg)
    async with get_sessionmaker()() as s:
        set_org_context(s, org.id)
        r = await s.get(TenDlcRegistration, reg.id)
        c = await s.get(Campaign, campaign.id)
        await tendlc._step(s, _settings(), r, None)
        await s.commit()
        assert r.stage == "brand_approved" and c.status == "draft"
    async with get_sessionmaker()() as s:
        await tendlc.cancel(s, _settings(), reg.id)
    assert stripe_stub.Refund.create.call_args.kwargs["amount"] == 1800 + 3 * 1250


async def test_refund_unused_for_reused_never_counts_the_brand_fee(stripe_stub):
    reg = TenDlcRegistration(fee_tier="standard", subscription_id="sub_x")
    reg.filing = {"brand_reused": True}
    reg.id = uuid.uuid4()
    text = await tendlc._refund_unused(_settings(), reg)
    assert stripe_stub.Refund.create.call_args.kwargs["amount"] == 1800 + 3 * 1250
    assert "$5.00 for the registration filing" in text


async def test_carrier_events_prefer_the_newest_registration_for_a_shared_brand():
    import inspect

    assert "created_at.desc()" in inspect.getsource(tendlc.handle_carrier_event)
