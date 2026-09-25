"""The whole customer journey, through the real API, in one test.

Sign up -> confirm email -> identity application -> super-admin approval -> buy numbers
(with the 911 address) -> the workspace becomes ready -> add a teammate on the second
number -> each person sees exactly the inboxes they should, and calling is allowed.

Only the outside world is faked: Didit (identity), Stripe (payment), Telnyx (numbers and
E911). Every Ringlite rule in between runs for real, so a regression anywhere on the path
a paying customer walks turns this red.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import OrgNumber
from app.providers.numbers import OrderResult
from app.services import e911, number_purchases, stripe_client, telephony_access
from tests.conftest import auth_headers
from tests.test_individual_kyc_api import _accept_agreement, _verify_owner
from tests.test_p41_kyc import _make_operator, _write_sanctions
from tests.test_p41_kyc import kyc_app as kyc_app  # noqa: F401 - fixture
from tests.test_p41_kyc import kyc_settings as kyc_settings  # noqa: F401 - fixture

PASSWORD = "correct-horse-battery"
NUMBERS = ["+15125550101", "+15125550102"]
APPLICATION = {
    "legal_name": "Ada Solo",
    "country": "US",
    "phone": "+15125550199",
    "industry": "Consulting",
    "business_description": "I advise small businesses on bookkeeping.",
    "purpose": "Calling and texting my clients about appointments.",
    "customer_country": "US",
}
ADDRESS = {
    "name": "Ada Solo Consulting",
    "street_address": "600 Congress Ave",
    "locality": "Austin",
    "administrative_area": "TX",
    "postal_code": "78701",
    "country_code": "US",
}


@pytest.fixture
def outside_world(monkeypatch):
    """Stripe says paid; Telnyx sells the numbers and registers E911."""
    purchase_ids: dict = {}

    def checkout_create(**params):
        purchase_ids["current"] = params["metadata"]["purchase_id"]
        return {"id": "cs_journey", "url": "https://checkout.stripe.test/cs_journey"}

    stripe = SimpleNamespace(
        Price=SimpleNamespace(
            retrieve=Mock(
                return_value={
                    "active": True,
                    "unit_amount": 1500,
                    "currency": "usd",
                    "recurring": {"interval": "month", "interval_count": 1},
                }
            )
        ),
        checkout=SimpleNamespace(
            Session=SimpleNamespace(
                create=Mock(side_effect=checkout_create),
                retrieve=Mock(
                    side_effect=lambda _id: {
                        "status": "complete",
                        "payment_status": "paid",
                        "subscription": "sub_journey",
                        "metadata": {"purchase_id": purchase_ids["current"]},
                    }
                ),
            )
        ),
        Subscription=SimpleNamespace(
            retrieve=Mock(
                return_value={
                    "id": "sub_journey",
                    "status": "active",
                    "items": {
                        "data": [{"price": {"id": "price_1UJHkZ744iNFjjqnkirIkrGn"}, "quantity": 2}]
                    },
                }
            )
        ),
    )
    monkeypatch.setattr(stripe_client, "_stripe", lambda settings: stripe)

    async def plenty(settings):
        return 100_000

    monkeypatch.setattr(number_purchases, "telnyx_available_cents", plenty)

    async def fake_telnyx(method, path, key, *, client=None, **kwargs):
        if path == "/addresses/actions/validate":
            return {"result": "valid"}
        if path == "/addresses":
            return {"id": "addr_journey"}
        if path == "/phone_numbers":
            return [{"id": "pn_" + kwargs["params"]["filter[phone_number]"][-4:]}]
        if path.endswith("/enable_emergency"):
            return {"emergency": {"emergency_status": "provisioning"}}
        raise AssertionError(f"unexpected Telnyx call {method} {path}")

    monkeypatch.setattr(e911, "_telnyx", fake_telnyx)

    async def api_key(session, settings):
        return "telnyx-test"

    monkeypatch.setattr(e911, "_api_key", api_key)

    carrier = SimpleNamespace(
        name="telnyx",
        order_number=AsyncMock(
            side_effect=lambda e164: OrderResult(e164=e164, provider_ref="ord", status="active")
        ),
    )
    registry = SimpleNamespace(get=lambda name: carrier)
    monkeypatch.setattr("app.providers.numbers.as_provider", lambda obj: obj)
    monkeypatch.setattr(
        "app.providers.registry_org.prime_org_registry", AsyncMock(return_value=registry)
    )
    return SimpleNamespace(stripe=stripe, carrier=carrier)


async def _step(client, h) -> str:
    r = await client.get("/api/v1/me/capabilities", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["org"]["onboarding_step"]


async def test_sign_up_to_a_working_inbox(
    kyc_app,  # noqa: F811
    kyc_settings,  # noqa: F811
    session,
    monkeypatch,
    outside_world,
):
    from tests.conftest import confirm_registered_email

    client, app, *_ = kyc_app
    _write_sanctions(kyc_settings, ["SOMEONE ELSE"])

    # 1. Sign up. Until the email is confirmed nothing but auth works.
    r = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "ada@journey-example.com",
            "password": PASSWORD,
            "account_type": "individual",
            "full_name": "Ada Solo",
        },
    )
    assert r.status_code == 201, r.text
    early = await client.post(
        "/api/v1/auth/login", json={"email": "ada@journey-example.com", "password": PASSWORD}
    )
    early_h = auth_headers(early.json()["access_token"])
    blocked = await client.get("/api/v1/kyc/profile", headers=early_h)
    assert blocked.json()["error"]["code"] == "email_verification_required"

    await confirm_registered_email(client, "ada@journey-example.com")
    token = (
        await client.post(
            "/api/v1/auth/login", json={"email": "ada@journey-example.com", "password": PASSWORD}
        )
    ).json()["access_token"]
    me = (await client.get("/api/v1/auth/me", headers=auth_headers(token))).json()
    org_id = me["memberships"][0]["org_id"]
    h = auth_headers(token, org_id)
    assert await _step(client, h) == "verification"

    # 2. Identity application.
    r = await client.put("/api/v1/kyc/application", json=APPLICATION, headers=h)
    assert r.status_code == 200, r.text
    person_id = r.json()["persons"][0]["id"]
    await _verify_owner(client, session, kyc_settings, monkeypatch, h, org_id, person_id)
    await _accept_agreement(client, h)
    r = await client.post("/api/v1/kyc/submit", headers=h)
    assert r.status_code == 200, r.text
    assert await _step(client, h) == "awaiting_review"

    checkout = {"numbers": NUMBERS, "acknowledge_e911": True, "emergency_address": ADDRESS}
    early_buy = await client.post("/api/v1/billing/number-checkout", json=checkout, headers=h)
    assert early_buy.status_code == 422
    assert "approved" in early_buy.json()["error"]["message"]

    # 3. A super admin approves it.
    admin = auth_headers(await _make_operator(client, session, "admin@journey-example.com"))
    r = await client.post(
        f"/api/v1/ops/applications/{org_id}/approve", json={"note": "Checked."}, headers=admin
    )
    assert r.status_code == 200, r.text
    assert await _step(client, h) == "numbers"  # the console sends them to /choose-numbers

    # 4. Buy two numbers, with the 911 address.
    r = await client.post("/api/v1/billing/number-checkout", json=checkout, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["checkout_url"] == "https://checkout.stripe.test/cs_journey"
    assert r.json()["monthly_total_cents"] == 3000
    purchase_id = r.json()["id"]
    r = await client.post(f"/api/v1/billing/number-purchases/{purchase_id}/complete", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "complete", r.json()
    assert await _step(client, h) == "ready"  # the console opens the inbox

    numbers = (await client.get("/api/v1/numbers", headers=h)).json()
    assert sorted(n["e164"] for n in numbers) == NUMBERS
    assert {n["emergency_status"] for n in numbers} == {"provisioning"}

    # 5. Two numbers = two people: the owner and one teammate, on the second number.
    inboxes = (await client.get("/api/v1/inboxes", headers=h)).json()
    assert sorted(i["e164"] for i in inboxes) == NUMBERS  # the owner sees every number
    second = next(i for i in inboxes if i["e164"] == NUMBERS[1])
    r = await client.post(
        "/api/v1/orgs/current/members",
        json={
            "email": "bob@journey-example.com",
            "full_name": "Bob Teammate",
            "password": PASSWORD,
            "role_name": "agent",
            "inbox_ids": [second["id"]],
        },
        headers=h,
    )
    assert r.status_code == 201, r.text
    third = await client.post(
        "/api/v1/orgs/current/members",
        json={"email": "cy@journey-example.com", "password": PASSWORD, "role_name": "agent"},
        headers=h,
    )
    assert third.json()["error"]["code"] == "seat_limit_reached"

    # 6. The teammate signs in with their own login and sees only their number.
    bob = (
        await client.post(
            "/api/v1/auth/login", json={"email": "bob@journey-example.com", "password": PASSWORD}
        )
    ).json()["access_token"]
    bob_h = auth_headers(bob, org_id)
    bob_inboxes = (await client.get("/api/v1/inboxes", headers=bob_h)).json()
    assert [i["e164"] for i in bob_inboxes] == [NUMBERS[1]]

    # 7. Calling is allowed for the approved, paying workspace.
    set_org_context(session, uuid.UUID(org_id))
    assert await telephony_access.telephony_allowed(session, uuid.UUID(org_id), "call") is None
    held = (
        (await session.execute(sa.select(OrgNumber).where(OrgNumber.org_id == uuid.UUID(org_id))))
        .scalars()
        .all()
    )
    assert all(n.is_active and n.status == "active" for n in held)
