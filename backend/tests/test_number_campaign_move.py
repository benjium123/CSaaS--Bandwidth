"""10DLC phase 4: move a Telnyx number from campaign A to approved campaign B.

Telnyx is an httpx.MockTransport that records every request, so the order of carrier calls
(GET campaign, DELETE old assignment, POST new assignment) is asserted, not assumed.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.db.session import get_sessionmaker
from app.errors import ConflictError, ValidationFailedError
from app.models import Brand, Campaign, Org, OrgNumber, SecurityAlert, TenDlcRegistration
from app.models.tendlc_event import TenDlcEvent
from app.services import registration as registration_svc
from app.services import telnyx_number_association as svc
from app.services import tendlc
from tests.conftest import auth_headers, make_org_with_number, make_settings

pytestmark = pytest.mark.asyncio

CARRIER_A = "carrier-campaign-A"
CARRIER_B = "carrier-campaign-B"
E164 = "+12025550123"


def _tx_settings():
    return make_settings(telnyx_api_key="test-telnyx-key")


class FakeTelnyx:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.delete_status = 200
        self.post_status = 200
        self.campaign_status = "MNO_PROVISIONED"

    @property
    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and "/10dlc/campaign/" in path:
            cid = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200, json={"campaignId": cid, "campaignStatus": self.campaign_status}
            )
        if request.method == "DELETE":
            if self.delete_status == 200:
                return httpx.Response(200, json={"phoneNumber": E164})
            return httpx.Response(self.delete_status, json={"errors": [{"detail": "x"}]})
        if request.method == "POST":
            if self.post_status != 200:
                return httpx.Response(
                    self.post_status, json={"errors": [{"code": "10999", "detail": "no"}]}
                )
            body = json.loads(request.content)
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={})


@pytest.fixture
async def telnyx():
    fake = FakeTelnyx()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as client:
        fake.client = client
        yield fake


async def _world(session, *, number_on="A", with_reg=True):
    """Org with two approved Telnyx campaigns A and B and one number on A."""
    org = Org(name="Move Org", slug=uuid.uuid4().hex)
    session.add(org)
    await session.flush()
    set_org_context(session, org.id)
    brand = Brand(org_id=org.id, name="Brand")
    session.add(brand)
    await session.flush()
    camp_a = Campaign(
        org_id=org.id, brand_id=brand.id, name="A", status="approved",
        carrier_refs={"telnyx": CARRIER_A},
    )
    camp_b = Campaign(
        org_id=org.id, brand_id=brand.id, name="B", status="approved",
        carrier_refs={"telnyx": CARRIER_B},
    )
    session.add_all([camp_a, camp_b])
    await session.flush()
    number = OrgNumber(
        org_id=org.id, e164=E164, carrier="telnyx", number_type="local", status="active",
        is_active=True,
        campaign_id=camp_a.id if number_on == "A" else None,
        provisioning={svc.ATTEMPT_KEY: {"state": "assigned"}} if number_on == "A" else {},
    )
    session.add(number)
    reg = None
    if with_reg:
        reg = TenDlcRegistration(
            org_id=org.id, brand_id=brand.id, campaign_id=camp_b.id, fee_tier="standard",
            stage="active",
        )
        session.add(reg)
    await session.commit()
    return org, camp_a, camp_b, number, reg


async def _events(session, org_id):
    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(TenDlcEvent)
            .where(TenDlcEvent.event_type == "local.number.move")
            .order_by(TenDlcEvent.received_at)
        )
    ).scalars().all()
    return [(r.payload_type, r.status) for r in rows], rows


async def test_move_happy_path(session, telnyx):
    org, camp_a, camp_b, number, reg = await _world(session)
    result = await svc.move_number_to_campaign(
        session, _tx_settings(), number, camp_b, client=telnyx.client
    )
    deletes = [p for m, p in telnyx.calls if m == "DELETE"]
    assert len(deletes) == 1 and deletes[0].endswith("/10dlc/phone_number_campaigns/+12025550123")
    assert [m for m, _ in telnyx.calls if m != "GET"] == ["DELETE", "POST"]
    await session.refresh(result)
    assert result.campaign_id == camp_b.id
    assert svc.MOVE_KEY not in result.provisioning
    marker = result.provisioning[svc.ATTEMPT_KEY]
    assert marker["state"] == svc.MARKER_STATE_ASSIGNED
    assert marker["carrier_campaign_id"] == CARRIER_B
    summary, rows = await _events(session, org.id)
    assert summary == [("UNASSIGN", "OK"), ("ASSIGN", "OK")]
    assert all(r.registration_id == reg.id for r in rows)


async def test_delete_404_is_success(session, telnyx):
    org, camp_a, camp_b, number, _ = await _world(session)
    telnyx.delete_status = 404
    result = await svc.move_number_to_campaign(
        session, _tx_settings(), number, camp_b, client=telnyx.client
    )
    await session.refresh(result)
    assert result.campaign_id == camp_b.id
    assert [m for m, _ in telnyx.calls if m != "GET"] == ["DELETE", "POST"]


async def test_post_refused_after_delete_leaves_number_unassigned(session, telnyx):
    org, camp_a, camp_b, number, _ = await _world(session)
    telnyx.post_status = 400
    with pytest.raises(ValidationFailedError) as exc:
        await svc.move_number_to_campaign(
            session, _tx_settings(), number, camp_b, client=telnyx.client
        )
    assert "removed from its old campaign" in str(exc.value)
    assert "can't text" in str(exc.value)
    await session.refresh(number)
    assert number.campaign_id is None
    assert number.provisioning[svc.MOVE_KEY]["state"] == svc.MOVE_STATE_FAILED
    assert number.provisioning[svc.MOVE_KEY]["error"]
    summary, _ = await _events(session, org.id)
    assert summary == [("UNASSIGN", "OK"), ("ASSIGN", "FAILED")]
    alerts = (
        await session.execute(
            sa.select(SecurityAlert).where(SecurityAlert.kind == "tendlc_number_move_failed")
        )
    ).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].detail["number_off_campaign"] is True


async def test_failed_delete_keeps_the_old_campaign(session, telnyx):
    org, camp_a, camp_b, number, _ = await _world(session)
    telnyx.delete_status = 400
    with pytest.raises(ValidationFailedError):
        await svc.move_number_to_campaign(
            session, _tx_settings(), number, camp_b, client=telnyx.client
        )
    await session.refresh(number)
    assert number.campaign_id == camp_a.id
    assert svc.MOVE_KEY not in number.provisioning
    assert [m for m, _ in telnyx.calls if m != "GET"] == ["DELETE"]
    summary, _ = await _events(session, org.id)
    assert summary == [("UNASSIGN", "FAILED")]


async def test_unapproved_target_is_refused_before_any_delete(session, telnyx):
    org, camp_a, camp_b, number, _ = await _world(session)
    telnyx.campaign_status = "TCR_PENDING"
    with pytest.raises(ValidationFailedError):
        await svc.move_number_to_campaign(
            session, _tx_settings(), number, camp_b, client=telnyx.client
        )
    assert all(m == "GET" for m, _ in telnyx.calls)
    await session.refresh(number)
    assert number.campaign_id == camp_a.id


async def test_a_number_with_no_campaign_is_not_moved(session, telnyx):
    org, camp_a, camp_b, number, _ = await _world(session, number_on=None)
    with pytest.raises(ConflictError):
        await svc.move_number_to_campaign(
            session, _tx_settings(), number, camp_b, client=telnyx.client
        )
    assert telnyx.calls == []


# ---- route level ---------------------------------------------------------------------
async def _route_world(app_with_carrier, session, monkeypatch, telnyx):
    client, _fake, _app = app_with_carrier
    token, org, number = await make_org_with_number(client, "mv1@example.com", "Org M", E164)
    h = auth_headers(token, org["id"])
    org_id = uuid.UUID(org["id"])
    async with get_sessionmaker()() as s:
        s.info["org_id"] = org_id
        brand = Brand(org_id=org_id, name="B")
        s.add(brand)
        await s.flush()
        ca = Campaign(org_id=org_id, brand_id=brand.id, name="A", status="approved",
                      carrier_refs={"telnyx": CARRIER_A})
        cb = Campaign(org_id=org_id, brand_id=brand.id, name="B", status="approved",
                      carrier_refs={"telnyx": CARRIER_B})
        s.add_all([ca, cb])
        await s.flush()
        row = await s.get(OrgNumber, uuid.UUID(number["id"]))
        row.carrier = "telnyx"
        row.number_type = "local"
        row.campaign_id = ca.id
        row.provisioning = {svc.ATTEMPT_KEY: {"state": "assigned"}}
        await s.commit()
        ids = (ca.id, cb.id)

    original = svc.move_number_to_campaign

    async def with_client(sess, _settings, num, camp):
        return await original(sess, _tx_settings(), num, camp, client=telnyx.client)

    monkeypatch.setattr(svc, "move_number_to_campaign", with_client)
    return client, h, number["id"], ids


async def test_route_moves_a_number(app_with_carrier, session, monkeypatch, telnyx):
    client, h, nid, (ca, cb) = await _route_world(app_with_carrier, session, monkeypatch, telnyx)
    r = await client.patch(
        f"/api/v1/numbers/{nid}/campaign", json={"campaign_id": str(cb)}, headers=h
    )
    assert r.status_code == 200, r.text
    assert [m for m, _ in telnyx.calls if m != "GET"] == ["DELETE", "POST"]
    async with get_sessionmaker()() as s:
        s.info["org_id"] = uuid.UUID(h["X-Org-Id"])
        row = await s.get(OrgNumber, uuid.UUID(nid))
        assert row.campaign_id == cb


async def test_route_refusal_after_delete_is_422(app_with_carrier, session, monkeypatch, telnyx):
    client, h, nid, (ca, cb) = await _route_world(app_with_carrier, session, monkeypatch, telnyx)
    telnyx.post_status = 400
    r = await client.patch(
        f"/api/v1/numbers/{nid}/campaign", json={"campaign_id": str(cb)}, headers=h
    )
    assert r.status_code == 422, r.text
    assert "removed from its old campaign" in r.json()["error"]["message"]
    async with get_sessionmaker()() as s:
        s.info["org_id"] = uuid.UUID(h["X-Org-Id"])
        row = await s.get(OrgNumber, uuid.UUID(nid))
        assert row.campaign_id is None


async def test_route_full_target_is_refused_before_the_carrier(
    app_with_carrier, session, monkeypatch, telnyx
):
    client, h, nid, (ca, cb) = await _route_world(app_with_carrier, session, monkeypatch, telnyx)
    monkeypatch.setattr(registration_svc, "MAX_NUMBERS_PER_CAMPAIGN", 1)
    org_id = uuid.UUID(h["X-Org-Id"])
    async with get_sessionmaker()() as s:
        s.info["org_id"] = org_id
        s.add(OrgNumber(org_id=org_id, e164="+12025550999", carrier="telnyx",
                        number_type="local", status="active", is_active=True, campaign_id=cb))
        await s.commit()
    r = await client.patch(
        f"/api/v1/numbers/{nid}/campaign", json={"campaign_id": str(cb)}, headers=h
    )
    assert r.status_code == 409, r.text
    assert telnyx.calls == []
    async with get_sessionmaker()() as s:
        s.info["org_id"] = org_id
        assert (await s.get(OrgNumber, uuid.UUID(nid))).campaign_id == ca


async def test_route_full_sole_proprietor_target_is_refused_before_the_carrier(
    app_with_carrier, session, monkeypatch, telnyx
):
    client, h, nid, (ca, cb) = await _route_world(app_with_carrier, session, monkeypatch, telnyx)
    org_id = uuid.UUID(h["X-Org-Id"])
    async with get_sessionmaker()() as s:
        s.info["org_id"] = org_id
        (await s.get(Campaign, cb)).use_case = "SOLE_PROPRIETOR"
        s.add(OrgNumber(org_id=org_id, e164="+12025550998", carrier="telnyx",
                        number_type="local", status="active", is_active=True, campaign_id=cb))
        await s.commit()
    r = await client.patch(
        f"/api/v1/numbers/{nid}/campaign", json={"campaign_id": str(cb)}, headers=h
    )
    assert r.status_code == 409, r.text
    assert "sole proprietor" in r.json()["error"]["message"]
    assert telnyx.calls == []
    async with get_sessionmaker()() as s:
        s.info["org_id"] = org_id
        assert (await s.get(OrgNumber, uuid.UUID(nid))).campaign_id == ca


async def test_route_clearing_a_telnyx_campaign_is_still_refused(
    app_with_carrier, session, monkeypatch, telnyx
):
    client, h, nid, (ca, cb) = await _route_world(app_with_carrier, session, monkeypatch, telnyx)
    r = await client.patch(f"/api/v1/numbers/{nid}/campaign", json={"campaign_id": None}, headers=h)
    assert r.status_code == 409
    assert telnyx.calls == []


# ---- new numbers ---------------------------------------------------------------------
async def test_two_approved_campaigns_means_new_numbers_get_nothing(session, monkeypatch):
    org, camp_a, camp_b, number, reg = await _world(session, number_on=None)
    called = []

    async def spy(*a, **k):
        called.append(1)
        return 0

    monkeypatch.setattr(tendlc, "associate_numbers", spy)
    await tendlc.associate_new_numbers(session, _tx_settings(), org.id)
    assert called == []


async def test_one_approved_campaign_still_auto_associates(session, monkeypatch):
    org, camp_a, camp_b, number, reg = await _world(session, number_on=None)
    camp_a.status = "rejected"
    await session.commit()
    called = []

    async def spy(*a, **k):
        called.append(1)
        return 0

    monkeypatch.setattr(tendlc, "associate_numbers", spy)
    await tendlc.associate_new_numbers(session, _tx_settings(), org.id)
    assert called == [1]
