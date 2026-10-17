"""Add another campaign: any number of active registrations, one in flight, numbers are
never grabbed by a second campaign."""

from __future__ import annotations

import pytest

from app.db.base import set_org_context
from app.errors import ConflictError
from app.models import Campaign, TenDlcRegistration
from app.services import tendlc
from tests.test_tendlc_notices import _api_org
from tests.test_tendlc_self_serve import (  # noqa: F401
    _checkout,
    _settings,
    _workspace,
    stripe_stub,
    telnyx,
)


async def _with_active(session, *, sole=False):
    """A workspace whose first campaign is approved and active, plus a second draft one."""
    org, brand, first, number = await _workspace(session, sole=sole)
    brand.status = "approved"
    brand.carrier_refs = {"telnyx": "B123"}
    first.status = "approved"
    first.carrier_refs = {"telnyx": "C_FIRST"}
    number.campaign_id = first.id
    reg = TenDlcRegistration(
        org_id=org.id, brand_id=brand.id, campaign_id=first.id,
        fee_tier="sole_proprietor" if sole else "standard", stage="active", filing={},
    )
    second = Campaign(
        org_id=org.id, brand_id=brand.id, name="Second", use_case=first.use_case,
        description=first.description, opt_in_process=first.opt_in_process,
        sample_messages=first.sample_messages, help_message="Reply HELP for help.",
        opt_out_message="You are unsubscribed.",
    )
    session.add_all([reg, second])
    await session.commit()
    return org, brand, first, second, number, reg


async def test_second_campaign_checkout_is_campaign_only_price(session, stripe_stub):
    org, brand, _, second, _, _ = await _with_active(session)
    reg = await _checkout(session, org, brand, second)
    params = stripe_stub.checkout.Session.create.call_args.kwargs
    assert params["line_items"][1]["price_data"]["unit_amount"] == 1800 + 500 + 3 * 1250 == 6050
    assert reg.filing["brand_reused"] is True and reg.stage == "checkout"


async def test_second_campaign_refused_while_another_is_in_flight(session, stripe_stub):
    org, brand, _, second, _, active = await _with_active(session)
    in_flight = TenDlcRegistration(
        org_id=org.id, brand_id=brand.id, campaign_id=second.id, fee_tier="standard",
        stage="campaign_filed", filing={},
    )
    session.add(in_flight)
    await session.commit()
    with pytest.raises(ConflictError):
        await _checkout(session, org, brand, second)


async def test_sole_proprietor_gets_one_campaign(session, stripe_stub):
    org, brand, _, second, _, _ = await _with_active(session, sole=True)
    with pytest.raises(ConflictError, match="can register one texting campaign"):
        await _checkout(session, org, brand, second, sub_usecases=["CUSTOMER_CARE"])


async def test_second_active_campaign_assigns_no_numbers(session, stripe_stub, telnyx):
    org, brand, first, second, number, _ = await _with_active(session)
    second.status = "approved"
    second.carrier_refs = {"telnyx": "C_SECOND"}
    reg2 = TenDlcRegistration(
        org_id=org.id, brand_id=brand.id, campaign_id=second.id, fee_tier="standard",
        stage="active", filing={},
    )
    session.add_all([reg2])
    await session.commit()
    set_org_context(session, org.id)
    assert await tendlc.associate_numbers(
        session, _settings(), reg2, second, client=telnyx.client
    ) == 0
    await session.refresh(number)
    assert number.campaign_id == first.id
    assert not telnyx.posts("/phone_number_campaigns")


async def test_get_lists_both_and_can_add_campaign(client, session):
    headers, org_id = await _api_org(client, "more-camp@example.com")
    r = await client.get("/api/v1/registration/texting", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["registrations"] == [] and r.json()["can_add_campaign"] is False

    from app.models import Brand

    set_org_context(session, org_id)
    brand = Brand(
        org_id=org_id, name="Api Co", ein="12-3456789", entity_type="PRIVATE_PROFIT",
        vertical="PROFESSIONAL", website="https://x.test", email="a@x.test",
        phone="+12125550100", street="1 Main", city="Austin", state="TX",
        postal_code="78701", country="US", status="approved",
        carrier_refs={"telnyx": "B1"},
    )
    session.add(brand)
    await session.flush()
    ids = []
    for name in ("c1", "c2"):
        c = Campaign(
            org_id=org_id, brand_id=brand.id, name=name, use_case="CUSTOMER_CARE",
            description="d" * 40, opt_in_process="o" * 40, sample_messages=["hi STOP"],
            help_message="h", opt_out_message="o", status="approved",
            carrier_refs={"telnyx": "C" + name},
        )
        session.add(c)
        await session.flush()
        ids.append(c.id)
        session.add(
            TenDlcRegistration(
                org_id=org_id, brand_id=brand.id, campaign_id=c.id, fee_tier="standard",
                stage="active", filing={},
            )
        )
    await session.commit()
    body = (await client.get("/api/v1/registration/texting", headers=headers)).json()
    assert len(body["registrations"]) == 2
    assert body["registration"]["id"] == body["registrations"][0]["id"]
    assert body["can_add_campaign"] is True

    session.add(
        TenDlcRegistration(
            org_id=org_id, brand_id=brand.id, campaign_id=ids[0], fee_tier="standard",
            stage="campaign_filed", filing={},
        )
    )
    await session.commit()
    body = (await client.get("/api/v1/registration/texting", headers=headers)).json()
    assert body["can_add_campaign"] is False


async def test_active_notice_tells_second_campaign_to_choose_numbers(session, stripe_stub):
    from app.services import tendlc_notices

    org, brand, _, second, _, reg = await _with_active(session)
    second.status = "approved"
    second.carrier_refs = {"telnyx": "C_SECOND"}
    await session.commit()
    set_org_context(session, org.id)
    assert await tendlc_notices._approved_campaigns(session, org.id) == 2
    assert "Numbers page" in tendlc_notices.body_for(
        reg, "active", "", tendlc_notices.CHOOSE_NUMBERS
    )
    assert "Numbers page" not in tendlc_notices.body_for(reg, "active", "")
