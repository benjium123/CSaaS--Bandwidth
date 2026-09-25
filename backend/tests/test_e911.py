"""E911: validated registered locations, per-number activation, direct 911 dialing that no
billing or verification gate can stop, the Kari's Law notice, and checkout's consent.

Telnyx is an httpx.MockTransport; LiveKit is a MockTransport that records the SIP dial.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.db.session import get_sessionmaker
from app.errors import ValidationFailedError
from app.main import create_app
from app.models import Call, CreditLedgerEntry, EmergencyAddress, Org, OrgNumber
from app.services import credits, e911, mailer, telephony_billing
from app.voice_plane import service as voice_service
from app.voice_plane.livekit_api import LiveKitApi
from tests.conftest import auth_headers, make_settings
from tests.test_voice_plane import (
    LK_KEY,
    LK_SECRET,
    make_livekit_settings,
)

OUR = "+12145550100"
ADDRESS = {
    "name": "Acme Dental",
    "street_address": "600 Congress Ave",
    "extended_address": "Suite 200",
    "locality": "Austin",
    "administrative_area": "tx",
    "postal_code": "78701",
    "country_code": "US",
}


def _settings():
    return make_settings(telnyx_api_key="telnyx-test-key")


class FakeTelnyx:
    def __init__(self, *, valid=True, enable_status=500):
        self.valid = valid
        self.enable_status = enable_status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/addresses/actions/validate"):
            body = {
                "result": "valid" if self.valid else "invalid",
                "suggested": {"street_address": "600 Congress Ave", "postal_code": "78701"},
            }
            return httpx.Response(200, json={"data": body})
        if path.endswith("/addresses"):
            return httpx.Response(200, json={"data": {"id": "addr_123"}})
        if path.endswith("/phone_numbers") and request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "pn_1"}]})
        if path.endswith("/actions/enable_emergency"):
            if self.enable_status >= 400:
                return httpx.Response(self.enable_status, json={"errors": [{"detail": "boom"}]})
            return httpx.Response(
                200, json={"data": {"emergency": {"emergency_status": "provisioning"}}}
            )
        if path.endswith("/voice"):
            return httpx.Response(200, json={"data": {"emergency": {"emergency_status": "active"}}})
        return httpx.Response(404, json={})

    def paths(self, suffix):
        return [r for r in self.requests if r.url.path.endswith(suffix)]


@pytest.fixture
async def telnyx():
    fake = FakeTelnyx()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as client:
        fake.client = client
        yield fake


async def _org_with_number(session, carrier="telnyx"):
    org = Org(id=uuid.uuid4(), name="E911 Co", slug=uuid.uuid4().hex)
    session.add(org)
    await session.commit()
    set_org_context(session, org.id)
    number = OrgNumber(
        org_id=org.id,
        e164=f"+1512555{uuid.uuid4().int % 10_000:04d}",
        carrier=carrier,
        number_type="local",
        status="active",
        is_active=True,
        provisioning={},
    )
    session.add(number)
    await session.commit()
    return org, number


# --------------------------------------------------------------------------------------
# Addresses and activation
# --------------------------------------------------------------------------------------
async def test_an_address_the_carrier_cannot_place_is_refused_before_registering(session, telnyx):
    org, _ = await _org_with_number(session)
    telnyx.valid = False
    with pytest.raises(ValidationFailedError, match="Did you mean: 600 Congress Ave, 78701"):
        await e911.create_address(session, _settings(), org.id, ADDRESS, client=telnyx.client)
    assert telnyx.paths("/addresses") == []


async def test_valid_address_is_registered_and_the_number_enabled(session, telnyx):
    org, number = await _org_with_number(session)
    address = await e911.create_address(session, _settings(), org.id, ADDRESS, client=telnyx.client)
    sent = json.loads(telnyx.paths("/addresses")[0].content)
    assert sent["business_name"] == "Acme Dental"
    assert (sent["extended_address"], sent["administrative_area"]) == ("Suite 200", "TX")
    assert address.telnyx_address_id == "addr_123"

    telnyx.enable_status = 200
    state = await e911.enable(session, _settings(), number, address, client=telnyx.client)
    body = json.loads(telnyx.paths("/actions/enable_emergency")[0].content)
    assert body == {"emergency_enabled": True, "emergency_address_id": "addr_123"}
    assert state["status"] == "provisioning"

    # The sweeper follows it to active.
    await e911.tick(get_sessionmaker(), _settings(), client=telnyx.client)
    async with get_sessionmaker()() as s:
        set_org_context(s, org.id)
        assert e911.status_of(await s.get(OrgNumber, number.id))["status"] == "active"


async def test_a_failed_activation_is_retried_by_the_sweeper(session, telnyx):
    org, number = await _org_with_number(session)
    address = await e911.create_address(session, _settings(), org.id, ADDRESS, client=telnyx.client)
    state = await e911.enable(session, _settings(), number, address, client=telnyx.client)
    assert state["status"] == "failed"

    telnyx.enable_status = 200
    counts = await e911.tick(get_sessionmaker(), _settings(), client=telnyx.client)
    assert counts["checked"] == 1
    assert len(telnyx.paths("/actions/enable_emergency")) == 2
    async with get_sessionmaker()() as s:
        set_org_context(s, org.id)
        assert e911.status_of(await s.get(OrgNumber, number.id))["status"] == "provisioning"


def test_other_carriers_report_unsupported_not_missing():
    number = OrgNumber(carrier="bandwidth", provisioning={})
    assert e911.status_of(number)["status"] == "unsupported"
    # P44e: SignalWire numbers register their address too.
    assert e911.status_of(OrgNumber(carrier="signalwire", provisioning={}))["status"] == "missing"


async def _room_org(session, client, email, org_name):
    """A new self-serve workspace (unverified, no subscription) holding one Telnyx number.
    The number is inserted directly: these workspaces may only buy through checkout."""
    from tests.conftest import create_org, register_and_login

    token = await register_and_login(client, email)
    org = await create_org(client, token, org_name)
    set_org_context(session, uuid.UUID(org["id"]))
    session.add(
        OrgNumber(
            org_id=uuid.UUID(org["id"]),
            e164=OUR,
            carrier="telnyx",
            number_type="local",
            status="active",
            is_active=True,
            provisioning={},
        )
    )
    await session.commit()
    return token, org, None


# --------------------------------------------------------------------------------------
# Dialing 911
# --------------------------------------------------------------------------------------
@pytest.fixture
async def app_with_dial_log(engine):
    application = create_app(make_livekit_settings())
    dials: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if "CreateSIPParticipant" in request.url.path:
            dials.append(json.loads(request.content))
        return httpx.Response(200, json={})

    lk_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application.state.livekit = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key=LK_KEY, api_secret=LK_SECRET, client=lk_client
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        yield client, dials
    await voice_service.wait_for_pending_dial_tasks()
    await lk_client.aclose()


async def test_911_connects_when_every_other_call_is_refused(app_with_dial_log, session):
    """A brand-new, unverified, unpaid workspace: an ordinary call is refused, 911 is not."""
    client, dials = app_with_dial_log
    token, org, _ = await _room_org(session, client, "e911-owner@example.com", "E911 Org")
    h = auth_headers(token, org["id"])
    mailer.outbox.clear()

    ordinary = await client.post(
        "/api/v1/calls", json={"to": "+19725550199", "via": "room"}, headers=h
    )
    assert ordinary.status_code in (402, 403), ordinary.text

    r = await client.post("/api/v1/calls", json={"to": "911", "via": "carrier"}, headers=h)
    assert r.status_code == 201, r.text
    await voice_service.wait_for_pending_dial_tasks()
    assert [d.get("sip_call_to") for d in dials] == ["911"]
    assert dials[0].get("sip_number") == OUR

    session.expire_all()
    call = (
        await session.execute(
            sa.select(Call)
            .where(Call.id == uuid.UUID(r.json()["id"]))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    assert call.extra.get("emergency") is True and call.tag == "emergency"

    # Kari's Law: the workspace is told, with the number it went out on.
    for _ in range(50):
        if mailer.outbox:
            break
        await asyncio.sleep(0.02)
    notice = next(m for m in mailer.outbox if "911 was dialed" in m["Subject"])
    assert "e911-owner@example.com" in notice["To"]
    assert OUR in notice.get_body(preferencelist=("plain",)).get_content()


async def test_933_test_calls_do_not_page_the_owners(app_with_dial_log, session):
    client, dials = app_with_dial_log
    token, org, _ = await _room_org(session, client, "e933@example.com", "E933 Org")
    mailer.outbox.clear()
    r = await client.post(
        "/api/v1/calls", json={"to": "933", "via": "room"}, headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 201, r.text
    await voice_service.wait_for_pending_dial_tasks()
    await asyncio.sleep(0.1)
    assert [d.get("sip_call_to") for d in dials] == ["933"]
    assert not [m for m in mailer.outbox if "911 was dialed" in m["Subject"]]


async def test_emergency_calls_are_never_billed(session):
    org = Org(id=uuid.uuid4(), name="Prepaid", slug=uuid.uuid4().hex)
    session.add(org)
    await session.commit()
    org.telephony_prepaid = True
    org.telephony_prepaid_since = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()
    set_org_context(session, org.id)
    await credits.topup(session, org.id, 5_000_000, reference=f"t-{uuid.uuid4()}")
    now = datetime.now(timezone.utc)
    call = Call(
        id=uuid.uuid4(),
        org_id=org.id,
        direction="outbound",
        contact_e164="911",
        our_e164=OUR,
        carrier="telnyx",
        status="completed",
        answered_at=now - timedelta(minutes=10),
        ended_at=now,
        duration_seconds=600,
        extra={"emergency": True},
    )
    session.add(call)
    await session.commit()

    await telephony_billing.bill_finished_calls(session)

    await session.refresh(call)
    assert call.billed_at is not None
    charges = (
        (
            await session.execute(
                sa.select(CreditLedgerEntry).where(
                    CreditLedgerEntry.org_id == org.id, CreditLedgerEntry.entry_type == "usage"
                )
            )
        )
        .scalars()
        .all()
    )
    assert charges == []


# --------------------------------------------------------------------------------------
# Checkout consent
# --------------------------------------------------------------------------------------
async def test_number_checkout_needs_the_911_acknowledgment_and_an_address(client):
    from tests.conftest import create_org, register_and_login

    token = await register_and_login(client, "e911-buyer@example.com")
    org = await create_org(client, token, "Buyer Org")
    h = auth_headers(token, org["id"])
    body = {"numbers": ["+12125550101"]}

    r = await client.post("/api/v1/billing/number-checkout", json=body, headers=h)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "e911_acknowledgment_required"

    r = await client.post(
        "/api/v1/billing/number-checkout", json={**body, "acknowledge_e911": True}, headers=h
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "e911_address_required"


async def test_purchase_records_the_address_and_when_limits_were_acknowledged(session):
    from unittest.mock import Mock

    from app.services import number_purchases, stripe_client
    from tests.test_number_purchases import approved_org

    org = await approved_org(session)
    address = EmergencyAddress(
        org_id=org.id,
        telnyx_address_id="addr_1",
        name="Acme",
        street_address="1 Main St",
        locality="Austin",
        administrative_area="TX",
        postal_code="78701",
        country_code="US",
    )
    session.add(address)
    await session.commit()
    stripe = type("S", (), {})()
    stripe.Price = type(
        "P",
        (),
        {
            "retrieve": Mock(
                return_value={
                    "active": True,
                    "unit_amount": 1500,
                    "currency": "usd",
                    "recurring": {"interval": "month", "interval_count": 1},
                }
            )
        },
    )
    stripe.checkout = type(
        "C",
        (),
        {
            "Session": type(
                "CS",
                (),
                {
                    "create": Mock(return_value={"id": "cs_1", "url": "https://pay"}),
                    "retrieve": Mock(),
                },
            )
        },
    )
    orig = stripe_client._stripe
    stripe_client._stripe = lambda settings: stripe
    try:
        purchase = await number_purchases.create(
            session,
            make_settings(stripe_webhook_secret="whsec"),
            org.id,
            ["+12125550133"],
            emergency_address_id=address.id,
            plan_code="solo",
        )
    finally:
        stripe_client._stripe = orig
    assert purchase.emergency_address_id == address.id
    assert purchase.e911_acknowledged_at is not None


async def test_a_member_who_cannot_place_calls_can_still_dial_911(app_with_dial_log, session):
    """Kari's Law: 911 is for anyone on the phone system, whatever their role."""
    from app.models import OrgMembership, Role

    client, dials = app_with_dial_log
    token, org, _ = await _room_org(session, client, "e911-reader@example.com", "Reader Org")
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    viewer = Role(id=uuid.uuid4(), org_id=org_id, name="viewer", permissions=["inbox:read"])
    session.add(viewer)
    await session.flush()
    membership = (
        await session.execute(sa.select(OrgMembership).where(OrgMembership.org_id == org_id))
    ).scalar_one()
    membership.role_id = viewer.id
    await session.commit()
    h = auth_headers(token, org["id"])

    ordinary = await client.post(
        "/api/v1/calls", json={"to": "+19725550199", "via": "room"}, headers=h
    )
    assert ordinary.status_code == 403
    assert "calls:place" in ordinary.json()["error"]["message"]

    r = await client.post("/api/v1/calls", json={"to": "911", "via": "room"}, headers=h)
    assert r.status_code == 201, r.text
    await voice_service.wait_for_pending_dial_tasks()
    assert [d.get("sip_call_to") for d in dials] == ["911"]
