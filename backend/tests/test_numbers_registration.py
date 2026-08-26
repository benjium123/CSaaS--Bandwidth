"""Phase 4: numbers, 10DLC campaigns, toll-free verification, and the pre-send gate.

The point of this phase is that an unregistered number is refused **before** the carrier is
called. So the load-bearing assertions are the ones that check `fake.sent == []` — proving
we stopped, rather than that we handled a rejection gracefully afterwards.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY
from app.models import OrgNumber
from app.models.numbers import Campaign
from app.services import registration as reg
from tests.conftest import auth_headers, make_org_with_number

LOCAL = "+12145550100"
LOCAL2 = "+12145550101"
TOLLFREE = "+18885550199"
CONTACT = "+19725559999"


async def _org(client, email="n1@example.com"):
    token, org, number = await make_org_with_number(client, email, "Org N", LOCAL)
    return token, org, auth_headers(token, org["id"]), number


async def _approved_campaign(client, h, name="Camp A") -> dict:
    """Brand -> approved; campaign -> approved. The happy path, in full."""
    brand = await client.post(
        "/api/v1/registration/brands",
        json={
            "name": f"Brand for {name}",
            "ein": "12-3456789",
            "email": "ops@example.com",
            "street": "1 Main St",
            "city": "Dallas",
            "state": "TX",
            "postal_code": "75201",
        },
        headers=h,
    )
    assert brand.status_code == 201, brand.text
    bid = brand.json()["id"]
    await client.post(f"/api/v1/registration/brands/{bid}/submit", headers=h)
    await client.post(
        f"/api/v1/registration/brands/{bid}/status", json={"status": "approved"}, headers=h
    )

    camp = await client.post(
        "/api/v1/registration/campaigns",
        json={
            "brand_id": bid,
            "name": name,
            "use_case": "MIXED",
            "description": "Property acquisition outreach",
            "opt_in_process": "Collected on our web form with an explicit checkbox",
            "sample_messages": ["Hi {{first_name}}, are you open to an offer on your lot?"],
            "opt_out_message": "Reply STOP to opt out.",
        },
        headers=h,
    )
    assert camp.status_code == 201, camp.text
    cid = camp.json()["id"]
    await client.post(f"/api/v1/registration/campaigns/{cid}/submit", headers=h)
    approved = await client.post(
        f"/api/v1/registration/campaigns/{cid}/status", json={"status": "approved"}, headers=h
    )
    assert approved.json()["status"] == "approved"
    return approved.json()


# ==================================================================================
# Monotonic registration status
# ==================================================================================
def test_a_late_submitted_cannot_undo_approved():
    """Carriers retry unordered. Demoting an approved campaign would silently stop every
    number on it from sending."""
    c = Campaign(id=uuid.uuid4(), org_id=uuid.uuid4(), brand_id=uuid.uuid4(), name="x")
    c.status = "submitted"
    assert reg.advance_status(c, "approved") is True
    assert reg.advance_status(c, "submitted") is False
    assert c.status == "approved"


def test_one_terminal_does_not_become_another():
    c = Campaign(id=uuid.uuid4(), org_id=uuid.uuid4(), brand_id=uuid.uuid4(), name="x")
    c.status = "approved"
    assert reg.advance_status(c, "rejected") is False
    assert c.status == "approved", "a duplicate callback must not flip an approval"


def test_an_unflushed_entity_is_treated_as_draft():
    """Column defaults land at INSERT, so a fresh object has status=None. Indexing that
    into the rank map would crash the very first transition."""
    c = Campaign(id=uuid.uuid4(), org_id=uuid.uuid4(), brand_id=uuid.uuid4(), name="x")
    assert c.status is None
    assert reg.advance_status(c, "approved") is True
    assert reg.advance_status(c, "draft") is False
    assert c.status == "approved"


def test_unknown_status_is_refused():
    from app.errors import ValidationFailedError

    c = Campaign(id=uuid.uuid4(), org_id=uuid.uuid4(), brand_id=uuid.uuid4(), name="x")
    with pytest.raises(ValidationFailedError):
        reg.advance_status(c, "probably_fine")


# ==================================================================================
# The pre-send gate
# ==================================================================================
async def test_pending_campaign_blocks_the_send_before_the_carrier(app_with_carrier, session):
    """THE LOAD-BEARING ONE. `fake.sent == []` is the whole point of the phase."""
    client, fake, _ = app_with_carrier
    token, org, h, number = await _org(client)

    brand = await client.post(
        "/api/v1/registration/brands",
        json={
            "name": "B", "ein": "12-3456789", "email": "e@x.com", "street": "1 Main",
            "city": "Dallas", "state": "TX", "postal_code": "75201",
        },
        headers=h,
    )
    bid = brand.json()["id"]
    await client.post(f"/api/v1/registration/brands/{bid}/submit", headers=h)
    await client.post(
        f"/api/v1/registration/brands/{bid}/status", json={"status": "approved"}, headers=h
    )
    camp = await client.post(
        "/api/v1/registration/campaigns",
        json={
            "brand_id": bid, "name": "C", "description": "d", "opt_in_process": "web form",
            "sample_messages": ["hi"], "opt_out_message": "Reply STOP",
        },
        headers=h,
    )
    cid = camp.json()["id"]
    await client.post(f"/api/v1/registration/campaigns/{cid}/submit", headers=h)

    link = await client.patch(
        f"/api/v1/numbers/{number['id']}/campaign", json={"campaign_id": cid}, headers=h
    )
    assert link.status_code == 200
    assert link.json()["registration"] == "pending"

    blocked = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h
    )
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["error"]["code"] == "compliance_blocked"
    assert fake.sent == [], "the carrier must never have been called"


async def test_approved_campaign_unblocks_the_send(app_with_carrier):
    client, fake, _ = app_with_carrier
    token, org, h, number = await _org(client, "n2@example.com")
    campaign = await _approved_campaign(client, h)
    await client.patch(
        f"/api/v1/numbers/{number['id']}/campaign",
        json={"campaign_id": campaign["id"]},
        headers=h,
    )

    sent = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)
    assert sent.status_code == 201, sent.text
    assert len(fake.sent) == 1


async def test_an_unregistered_number_still_sends_and_says_so(app_with_carrier):
    """A number we hold no registration for is allowed through - it may well be registered
    directly at the carrier. We refuse what we KNOW is wrong, not what we merely don't know.
    """
    client, fake, _ = app_with_carrier
    token, org, h, number = await _org(client, "n3@example.com")

    listed = await client.get("/api/v1/numbers", headers=h)
    assert listed.json()[0]["registration"] == "unknown"

    sent = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)
    assert sent.status_code == 201
    assert len(fake.sent) == 1


async def test_the_router_skips_an_ineligible_number(app_with_carrier):
    """With two numbers and only one registered, the send must pick the registered one."""
    client, fake, _ = app_with_carrier
    token, org, h, number = await _org(client, "n4@example.com")
    second = await client.post("/api/v1/numbers", json={"e164": LOCAL2}, headers=h)
    assert second.status_code == 201

    campaign = await _approved_campaign(client, h)
    # LOCAL gets an APPROVED campaign; LOCAL2 gets a PENDING one, so it is known-bad.
    await client.patch(
        f"/api/v1/numbers/{number['id']}/campaign",
        json={"campaign_id": campaign["id"]},
        headers=h,
    )
    pending = await client.post(
        "/api/v1/registration/campaigns",
        json={
            "brand_id": campaign["brand_id"], "name": "Pending Camp", "description": "d",
            "opt_in_process": "web", "sample_messages": ["s"], "opt_out_message": "STOP",
        },
        headers=h,
    )
    await client.patch(
        f"/api/v1/numbers/{second.json()['id']}/campaign",
        json={"campaign_id": pending.json()["id"]},
        headers=h,
    )

    for contact in ("+19725550001", "+19725550002", "+19725550003", "+19725550004"):
        r = await client.post("/api/v1/messages", json={"to": contact, "body": "hi"}, headers=h)
        assert r.status_code == 201, r.text
        assert r.json()["from_e164"] == LOCAL, "must never spread onto the unregistered number"


# ==================================================================================
# Toll-free is a different regime
# ==================================================================================
async def test_tollfree_gates_on_verification_not_on_a_campaign(app_with_carrier):
    client, fake, _ = app_with_carrier
    token, org, h, _ = await _org(client, "n5@example.com")
    tf = await client.post(
        "/api/v1/numbers", json={"e164": TOLLFREE, "number_type": "tollfree"}, headers=h
    )
    assert tf.status_code == 201
    tf_id = tf.json()["id"]

    tfv = await client.post(
        "/api/v1/registration/tollfree",
        json={
            "number_id": tf_id,
            "business_name": "Sabine Property Group",
            "use_case_summary": "Property acquisition outreach",
            "opt_in_process": "Web form with explicit checkbox",
        },
        headers=h,
    )
    assert tfv.status_code == 201, tfv.text
    tfv_id = tfv.json()["id"]
    await client.post(f"/api/v1/registration/tollfree/{tfv_id}/submit", headers=h)

    sending = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "from": TOLLFREE, "body": "hi"}, headers=h
    )
    assert sending.status_code == 422, "a submitted-but-unapproved TFV must block"
    assert fake.sent == []

    await client.post(
        f"/api/v1/registration/tollfree/{tfv_id}/status", json={"status": "approved"}, headers=h
    )
    ok = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "from": TOLLFREE, "body": "hi"}, headers=h
    )
    assert ok.status_code == 201, ok.text
    assert len(fake.sent) == 1


async def test_a_campaign_cannot_be_attached_to_a_tollfree_number(app_with_carrier):
    """Otherwise an 'approved' campaign would imply a toll-free number could send."""
    client, _, _ = app_with_carrier
    token, org, h, _ = await _org(client, "n6@example.com")
    tf = await client.post(
        "/api/v1/numbers", json={"e164": TOLLFREE, "number_type": "tollfree"}, headers=h
    )
    campaign = await _approved_campaign(client, h)

    r = await client.patch(
        f"/api/v1/numbers/{tf.json()['id']}/campaign",
        json={"campaign_id": campaign["id"]},
        headers=h,
    )
    assert r.status_code == 422
    assert "toll-free" in r.text.lower()


async def test_tfv_cannot_be_filed_against_a_local_number(app_with_carrier):
    client, _, _ = app_with_carrier
    token, org, h, number = await _org(client, "n7@example.com")
    r = await client.post(
        "/api/v1/registration/tollfree",
        json={"number_id": number["id"], "business_name": "X"},
        headers=h,
    )
    assert r.status_code == 422


# ==================================================================================
# Submission validation
# ==================================================================================
async def test_a_campaign_cannot_outrun_its_brand(app_with_carrier):
    client, _, _ = app_with_carrier
    token, org, h, _ = await _org(client, "n8@example.com")
    brand = await client.post(
        "/api/v1/registration/brands",
        json={
            "name": "B2", "ein": "12-3456789", "email": "e@x.com", "street": "1 Main",
            "city": "Dallas", "state": "TX", "postal_code": "75201",
        },
        headers=h,
    )
    camp = await client.post(
        "/api/v1/registration/campaigns",
        json={
            "brand_id": brand.json()["id"], "name": "C2", "description": "d",
            "opt_in_process": "web", "sample_messages": ["s"], "opt_out_message": "STOP",
        },
        headers=h,
    )
    r = await client.post(
        f"/api/v1/registration/campaigns/{camp.json()['id']}/submit", headers=h
    )
    assert r.status_code == 422
    assert "brand must be approved" in r.text.lower()


async def test_incomplete_registrations_are_refused_locally(app_with_carrier):
    """Vetting takes weeks; anything we can catch before submission we catch before it."""
    client, _, _ = app_with_carrier
    token, org, h, _ = await _org(client, "n9@example.com")

    brand = await client.post(
        "/api/v1/registration/brands", json={"name": "Thin"}, headers=h
    )
    assert brand.status_code == 201
    assert set(brand.json()["missing_for_submission"]) >= {"email", "street", "ein"}

    r = await client.post(
        f"/api/v1/registration/brands/{brand.json()['id']}/submit", headers=h
    )
    assert r.status_code == 422


async def test_sole_proprietor_does_not_need_an_ein(app_with_carrier):
    client, _, _ = app_with_carrier
    token, org, h, _ = await _org(client, "n10@example.com")
    brand = await client.post(
        "/api/v1/registration/brands",
        json={
            "name": "Solo", "entity_type": "SOLE_PROPRIETOR", "email": "e@x.com",
            "street": "1 Main", "city": "Dallas", "state": "TX", "postal_code": "75201",
        },
        headers=h,
    )
    assert brand.json()["missing_for_submission"] == []
    r = await client.post(
        f"/api/v1/registration/brands/{brand.json()['id']}/submit", headers=h
    )
    assert r.status_code == 200
    assert r.json()["status"] == "submitted"


# ==================================================================================
# Release keeps the evidence
# ==================================================================================
async def test_releasing_a_number_keeps_the_row_and_its_history(app_with_carrier, session):
    """The consent ledger is how we prove we honoured a STOP. It must outlive the number."""
    from app.models import ConsentEvent, MessageThread

    client, fake, _ = app_with_carrier
    token, org, h, number = await _org(client, "n11@example.com")
    await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)

    released = await client.delete(f"/api/v1/numbers/{number['id']}", headers=h)
    assert released.status_code == 200
    assert released.json()["status"] == "released"

    rows = list(
        (
            await session.execute(
                sa.select(OrgNumber)
                .where(OrgNumber.e164 == LOCAL)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
    )
    assert len(rows) == 1, "the row must survive"
    assert rows[0].released_at is not None

    threads = list(
        (
            await session.execute(
                sa.select(MessageThread)
                .where(MessageThread.our_e164 == LOCAL)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
    )
    assert len(threads) == 1, "its conversation history must survive too"
    assert (
        await session.execute(
            sa.select(ConsentEvent).execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ) is not None


async def test_a_released_number_cannot_send(app_with_carrier):
    client, fake, _ = app_with_carrier
    token, org, h, number = await _org(client, "n12@example.com")
    await client.delete(f"/api/v1/numbers/{number['id']}", headers=h)

    r = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)
    assert r.status_code in (422, 503)
    assert fake.sent == []


# ==================================================================================
# Provisioning capability
# ==================================================================================
def test_provisioning_capability_is_declared_not_probed():
    from app.errors import FeatureUnavailableError
    from app.providers.bandwidth.adapter import BandwidthMessagingCarrier
    from app.providers.numbers import NumberProvider, as_provider
    from app.providers.telnyx.adapter import TelnyxMessagingCarrier

    telnyx = TelnyxMessagingCarrier(api_key="k")
    assert isinstance(telnyx, NumberProvider)
    assert as_provider(telnyx) is telnyx

    bandwidth = BandwidthMessagingCarrier(
        account_id="a", api_username="u", api_password="p", application_id="x"
    )
    assert not isinstance(bandwidth, NumberProvider)
    with pytest.raises(FeatureUnavailableError) as exc:
        as_provider(bandwidth)
    assert "manually" in str(exc.value), "the error must tell the operator what to do instead"


async def test_telnyx_search_and_order_parse_the_real_shapes():
    import httpx

    from app.providers.numbers import NumberSearch
    from app.providers.telnyx.adapter import TelnyxMessagingCarrier

    def handler(request: httpx.Request) -> httpx.Response:
        if "available_phone_numbers" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "phone_number": "+12145550123",
                            "phone_number_type": "local",
                            "region_information": {
                                "administrative_area": "TX", "locality": "Dallas"
                            },
                            "cost_information": {"monthly_cost": "1.00", "upfront_cost": "1.00"},
                            "features": [{"name": "sms"}, {"name": "mms"}, {"name": "voice"}],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "status": "success",
                    "id": "order-1",
                    "phone_numbers": [
                        {
                            "id": "num-1",
                            "phone_number": "+12145550123",
                            "features": [{"name": "sms"}],
                        }
                    ],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = TelnyxMessagingCarrier(api_key="k", client=http)
        found = await carrier.search_numbers(NumberSearch(area_code="214"))
        assert len(found) == 1
        assert found[0].e164 == "+12145550123"
        assert found[0].capabilities == {"sms": True, "mms": True, "voice": True}

        order = await carrier.order_number("+12145550123")
        assert order.status == "active"
        assert order.provider_ref == "num-1"


async def test_a_pending_order_is_not_reported_as_active():
    """Recording a pending order as active silently drops inbound until someone asks why."""
    import httpx

    from app.providers.telnyx.adapter import TelnyxMessagingCarrier

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "status": "pending",
                    "phone_numbers": [{"id": "n1", "phone_number": "+12145550123"}],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        carrier = TelnyxMessagingCarrier(api_key="k", client=http)
        assert (await carrier.order_number("+12145550123")).status == "pending"


# ==================================================================================
# Tenancy
# ==================================================================================
async def test_registration_is_org_scoped(app_with_carrier):
    from tests.conftest import create_org, register_and_login

    client, _, _ = app_with_carrier
    token_a, org_a, h_a, _ = await _org(client, "n13@example.com")
    await _approved_campaign(client, h_a, name="Private Camp")

    token_b = await register_and_login(client, "n14@example.com")
    org_b = await create_org(client, token_b, "Org O")
    listed = await client.get(
        "/api/v1/registration/campaigns", headers=auth_headers(token_b, org_b["id"])
    )
    assert listed.json() == []
    brands = await client.get(
        "/api/v1/registration/brands", headers=auth_headers(token_b, org_b["id"])
    )
    assert brands.json() == []


async def test_brand_and_campaign_names_are_unique_per_org(app_with_carrier):
    client, _, _ = app_with_carrier
    token, org, h, _ = await _org(client, "n15@example.com")
    body = {"name": "Dup"}
    first = await client.post("/api/v1/registration/brands", json=body, headers=h)
    assert first.status_code == 201
    second = await client.post("/api/v1/registration/brands", json=body, headers=h)
    assert second.status_code == 409
