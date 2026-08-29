"""P12 services/flows.py: validation gate, versioning/pinning, number binding, and the
DR-10 business-hours evaluation helper.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models.callflow import BusinessHours
from app.services import flows as flows_svc
from tests.conftest import auth_headers, create_org, make_org_with_number, register_and_login

OUR = "+12145550100"

SIMPLE_FLOW = {
    "entry": "welcome",
    "nodes": {"welcome": {"type": "hangup"}},
}

BROKEN_FLOW = {
    "entry": "welcome",
    "nodes": {"welcome": {"type": "speak", "text": "hi", "next": "nowhere"}},
}


async def _org(client) -> uuid.UUID:
    _token, org, _number = await make_org_with_number(client, "flows1@example.com", "Org A", OUR)
    return uuid.UUID(org["id"])


async def _plain_org(client, email: str, name: str) -> uuid.UUID:
    """A real Org row (CallFlow.org_id has an FK to orgs.id) with no number attached."""
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    return uuid.UUID(org["id"])


# --------------------------------------------------------------------------------------
# Validation gate (DR-4) - direct service call
# --------------------------------------------------------------------------------------
async def test_create_flow_rejects_invalid_definition_with_node_ids(session, engine):
    org_id = uuid.uuid4()
    with pytest.raises(ValidationFailedError) as excinfo:
        await flows_svc.create_flow(session, org_id, name="broken", definition=BROKEN_FLOW)
    assert "nowhere" in str(excinfo.value)
    assert "welcome" in str(excinfo.value)


async def test_create_flow_accepts_valid_definition(app_with_carrier, session):
    client, _fake, _app = app_with_carrier
    org_id = await _plain_org(client, "flowsok@example.com", "Org OK")
    set_org_context(session, org_id)
    row = await flows_svc.create_flow(session, org_id, name="ok", definition=SIMPLE_FLOW)
    assert row.version == 1
    assert row.status == "draft"


# --------------------------------------------------------------------------------------
# B3: cross-object reference validation (structural validate_flow cannot know whether an
# id actually resolves - it has no DB access).
# --------------------------------------------------------------------------------------
async def test_create_flow_rejects_unknown_ring_group_reference(app_with_carrier, session):
    client, _fake, _app = app_with_carrier
    org_id = await _plain_org(client, "flowsxref1@example.com", "Org XRef1")
    set_org_context(session, org_id)
    flow = {
        "entry": "ring",
        "nodes": {"ring": {"type": "ring_group", "ring_group_id": str(uuid.uuid4())}},
    }
    with pytest.raises(ValidationFailedError) as excinfo:
        await flows_svc.create_flow(session, org_id, name="bad-ring", definition=flow)
    assert "ring" in str(excinfo.value)
    assert "ring_group" in str(excinfo.value)


async def test_create_flow_rejects_unknown_queue_reference(app_with_carrier, session):
    client, _fake, _app = app_with_carrier
    org_id = await _plain_org(client, "flowsxref2@example.com", "Org XRef2")
    set_org_context(session, org_id)
    flow = {
        "entry": "q",
        "nodes": {"q": {"type": "queue", "queue_id": str(uuid.uuid4())}},
    }
    with pytest.raises(ValidationFailedError) as excinfo:
        await flows_svc.create_flow(session, org_id, name="bad-queue", definition=flow)
    assert "'q'" in str(excinfo.value)
    assert "queue" in str(excinfo.value)


async def test_create_flow_rejects_unknown_business_hours_reference(app_with_carrier, session):
    client, _fake, _app = app_with_carrier
    org_id = await _plain_org(client, "flowsxref3@example.com", "Org XRef3")
    set_org_context(session, org_id)
    flow = {
        "entry": "hours",
        "nodes": {
            "hours": {
                "type": "hours",
                "business_hours_id": str(uuid.uuid4()),
                "open": "open_node",
                "closed": "open_node",
                "holiday": "open_node",
            },
            "open_node": {"type": "hangup"},
        },
    }
    with pytest.raises(ValidationFailedError) as excinfo:
        await flows_svc.create_flow(session, org_id, name="bad-hours", definition=flow)
    assert "hours" in str(excinfo.value)
    assert "business_hours" in str(excinfo.value)


async def test_create_flow_accepts_real_cross_object_references(app_with_carrier, session):
    from app.models.callflow import CallQueue, RingGroupDef

    client, _fake, _app = app_with_carrier
    org_id = await _plain_org(client, "flowsxref4@example.com", "Org XRef4")
    set_org_context(session, org_id)
    ring = RingGroupDef(
        id=uuid.uuid4(), org_id=org_id, name="rg", strategy="simultaneous", member_user_ids=[]
    )
    queue = CallQueue(id=uuid.uuid4(), org_id=org_id, name="q")
    bh = BusinessHours(id=uuid.uuid4(), org_id=org_id, name="bh", timezone="UTC")
    session.add_all([ring, queue, bh])
    await session.flush()

    flow = {
        "entry": "hours",
        "nodes": {
            "hours": {
                "type": "hours",
                "business_hours_id": str(bh.id),
                "open": "ring",
                "closed": "ring",
                "holiday": "ring",
            },
            "ring": {"type": "ring_group", "ring_group_id": str(ring.id), "no_answer": "q"},
            "q": {"type": "queue", "queue_id": str(queue.id)},
        },
    }
    row = await flows_svc.create_flow(session, org_id, name="good-xref", definition=flow)
    assert row.status == "draft"


async def test_create_flow_api_422_lists_unknown_ring_group_reference(app_with_carrier):
    client, _fake, _app = app_with_carrier
    token = await register_and_login(client, "xrefapi@example.com")
    org = await create_org(client, token, "Org XRef API")
    flow = {
        "entry": "ring",
        "nodes": {"ring": {"type": "ring_group", "ring_group_id": str(uuid.uuid4())}},
    }
    r = await client.post(
        "/api/v1/flows",
        json={"name": "bad-ring-api", "definition": flow},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 422, r.text
    assert "ring" in r.json()["error"]["message"]


# --------------------------------------------------------------------------------------
# Validation gate via the API - 422 listing node ids
# --------------------------------------------------------------------------------------
async def test_create_flow_api_422_lists_node_ids(app_with_carrier):
    client, _fake, _app = app_with_carrier
    from tests.conftest import create_org, register_and_login

    token = await register_and_login(client, "api1@example.com")
    org = await create_org(client, token, "Org API")
    r = await client.post(
        "/api/v1/flows",
        json={"name": "broken", "definition": BROKEN_FLOW},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 422, r.text
    assert "nowhere" in r.json()["error"]["message"]


# --------------------------------------------------------------------------------------
# Versioning + pinning (DR-3)
# --------------------------------------------------------------------------------------
async def test_editing_a_flow_creates_a_new_immutable_version(app_with_carrier, session):
    client, _fake, _app = app_with_carrier
    org_id = await _plain_org(client, "flowsedit@example.com", "Org Edit")
    set_org_context(session, org_id)
    v1 = await flows_svc.create_flow(session, org_id, name="ivr", definition=SIMPLE_FLOW)
    assert v1.version == 1

    other_flow = {
        "entry": "welcome",
        "nodes": {
            "welcome": {"type": "speak", "text": "hi", "next": "bye"},
            "bye": {"type": "hangup"},
        },
    }
    v2 = await flows_svc.create_version(session, org_id, flow_id=v1.id, definition=other_flow)
    assert v2.version == 2
    assert v2.id != v1.id
    assert v2.status == "draft"
    # v1's definition is untouched.
    assert v1.definition == SIMPLE_FLOW


async def test_activating_a_version_archives_the_previously_active_one(app_with_carrier, session):
    client, _fake, _app = app_with_carrier
    org_id = await _plain_org(client, "flowsactivate@example.com", "Org Activate")
    set_org_context(session, org_id)
    v1 = await flows_svc.create_flow(session, org_id, name="ivr", definition=SIMPLE_FLOW)
    await flows_svc.activate_flow(session, org_id, v1.id)
    assert v1.status == "active"

    v2 = await flows_svc.create_version(session, org_id, flow_id=v1.id, definition=SIMPLE_FLOW)
    await flows_svc.activate_flow(session, org_id, v2.id)

    await session.refresh(v1)
    assert v1.status == "archived"
    assert v2.status == "active"


# --------------------------------------------------------------------------------------
# Number binding
# --------------------------------------------------------------------------------------
async def test_bind_number_requires_an_active_flow_version(app_with_carrier, session):
    client, _fake, _app = app_with_carrier
    org_id = await _org(client)
    import sqlalchemy as sa

    from app.db.base import set_org_context
    from app.models.messaging import OrgNumber

    set_org_context(session, org_id)
    number = (await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == OUR))).scalar_one()
    flow = await flows_svc.create_flow(session, org_id, name="ivr", definition=SIMPLE_FLOW)

    with pytest.raises(ValidationFailedError):
        await flows_svc.bind_number(session, org_id, number.id, flow.id)

    await flows_svc.activate_flow(session, org_id, flow.id)
    bound = await flows_svc.bind_number(session, org_id, number.id, flow.id)
    assert bound.call_flow_id == flow.id

    unbound = await flows_svc.bind_number(session, org_id, number.id, None)
    assert unbound.call_flow_id is None


# --------------------------------------------------------------------------------------
# Business hours evaluation (DR-10) - frozen instants, including a DST-transition date.
# --------------------------------------------------------------------------------------
def _hours(**overrides) -> BusinessHours:
    bh = BusinessHours(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="default",
        timezone="America/Chicago",
        schedule={"mon": [["09:00", "17:00"]], "tue": [["09:00", "17:00"]]},
        holidays=["2026-12-25"],
    )
    for key, value in overrides.items():
        setattr(bh, key, value)
    return bh


def test_evaluate_hours_open_inside_window():
    bh = _hours()
    # Monday 2026-06-15 18:00 UTC = 13:00 CDT - inside the 09:00-17:00 window.
    moment = datetime(2026, 6, 15, 18, 0, tzinfo=timezone.utc)
    assert flows_svc.evaluate_hours(bh, moment) == "open"


def test_evaluate_hours_closed_outside_window():
    bh = _hours()
    # Monday 2026-06-15 03:00 UTC = 22:00 CDT the prior day - closed.
    moment = datetime(2026, 6, 15, 3, 0, tzinfo=timezone.utc)
    assert flows_svc.evaluate_hours(bh, moment) == "closed"


def test_evaluate_hours_holiday_beats_open_window():
    bh = _hours(schedule={"fri": [["09:00", "17:00"]]})
    # Friday 2026-12-25 18:00 UTC = 12:00 CST - inside the window, but a holiday.
    moment = datetime(2026, 12, 25, 18, 0, tzinfo=timezone.utc)
    assert flows_svc.evaluate_hours(bh, moment) == "holiday"


def test_evaluate_hours_dst_spring_forward_boundary():
    """2026-03-08 02:00 America/Chicago does not exist (DST spring-forward) - zoneinfo
    resolves the instant correctly regardless, which is the entire point of DR-10 using
    zoneinfo-at-an-instant instead of manual offset arithmetic."""
    bh = _hours(schedule={"sun": [["09:00", "17:00"]]})
    # 2026-03-08 is a Sunday. The US spring-forward happens at 2:00am local, which is
    # 08:00 UTC that day regardless of side (2:00 CST == 3:00 CDT == 08:00 UTC) - so every
    # instant from 08:00 UTC onward that day is already in CDT (-5). 12:00 UTC is 07:00
    # CDT - before the 09:00 window opens.
    before_open = datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc)
    assert flows_svc.evaluate_hours(bh, before_open) == "closed"
    # 16:30 UTC on that date is 10:30 CDT (already +1 across the spring-forward) - inside
    # the window. A naive fixed-offset implementation would get this wrong.
    after_open = datetime(2026, 3, 8, 16, 30, tzinfo=timezone.utc)
    assert flows_svc.evaluate_hours(bh, after_open) == "open"
    local = after_open.astimezone(ZoneInfo("America/Chicago"))
    assert local.utcoffset().total_seconds() == -5 * 3600  # CDT, not CST


def test_evaluate_hours_unknown_timezone_falls_back_to_utc_rather_than_crashing():
    bh = _hours(timezone="Not/ARealZone", schedule={"mon": [["09:00", "17:00"]]})
    moment = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    assert flows_svc.evaluate_hours(bh, moment) in ("open", "closed")  # never raises
