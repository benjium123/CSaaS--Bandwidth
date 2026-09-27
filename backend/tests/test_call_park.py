"""Call park / pickup on live softphone calls: hold music while parked, one pickup wins,
events reach only people who could pick it up, and the parked list respects number access."""

from __future__ import annotations

import types
import uuid

import pytest

from app.api.routes.softphone import _event_visible
from app.errors import ConflictError, FeatureUnavailableError
from app.services import call_hold, call_park
from app.services.inbox_access import InboxAccess

AGENT = types.SimpleNamespace(id=uuid.uuid4(), full_name="Ana Agent", email="ana@x.test")
MATE = types.SimpleNamespace(id=uuid.uuid4(), full_name="Sam Mate", email="sam@x.test")
SETTINGS = types.SimpleNamespace(hold_music_agent_name="hold-music")


class Bus:
    def __init__(self):
        self.events: list[dict] = []

    def publish(self, org_id, event):
        self.events.append(event)


class Session:
    async def commit(self):
        pass

    async def refresh(self, obj, with_for_update=False):
        pass


def _call(**extra):
    return types.SimpleNamespace(
        id=uuid.uuid4(), org_id=uuid.uuid4(), status="answered",
        contact_e164="+14695550100", our_e164="+14694617145",
        extra={"via": "livekit", "room": "call-1", **extra},
    )


@pytest.fixture
def holds(monkeypatch):
    calls: list[bool] = []

    async def set_hold(session, api, settings, call, *, on):
        calls.append(on)
        extra = dict(call.extra or {})
        if on:
            extra["on_hold"] = {"since": "now"}
        else:
            extra.pop("on_hold", None)
        call.extra = extra

    monkeypatch.setattr(call_hold, "set_hold", set_hold)
    return calls


@pytest.mark.asyncio
async def test_park_holds_the_caller_and_pickup_resumes(holds):
    bus, call = Bus(), _call()
    await call_park.park(Session(), bus, object(), SETTINGS, call, user=AGENT)
    assert holds == [True]
    assert call.extra["parked"]["by_name"] == "Ana Agent"
    event = bus.events[-1]
    assert event["type"] == "call.parked" and event["state"] == "parked"
    assert event["to"] == call.our_e164 and event["by"] == "Ana Agent"

    room = await call_park.pickup(Session(), bus, object(), SETTINGS, call, user=MATE)
    assert room == "call-1"
    assert holds == [True, False]
    assert "parked" not in call.extra
    assert bus.events[-1]["state"] == "picked_up" and bus.events[-1]["by"] == "Sam Mate"
    # Only one pickup wins.
    with pytest.raises(ConflictError):
        await call_park.pickup(Session(), bus, object(), SETTINGS, call, user=AGENT)


@pytest.mark.asyncio
async def test_cannot_park_twice_or_an_ended_call(holds):
    call = _call()
    await call_park.park(Session(), Bus(), object(), SETTINGS, call, user=AGENT)
    with pytest.raises(ConflictError):
        await call_park.park(Session(), Bus(), object(), SETTINGS, call, user=AGENT)
    ended = _call()
    ended.status = "completed"
    with pytest.raises(ConflictError):
        await call_park.park(Session(), Bus(), object(), SETTINGS, ended, user=AGENT)


@pytest.mark.asyncio
async def test_park_refused_without_hold_music(monkeypatch):
    async def no_music(session, api, settings, call, *, on):
        raise FeatureUnavailableError("no hold music")

    monkeypatch.setattr(call_hold, "set_hold", no_music)
    call = _call()
    with pytest.raises(ConflictError):
        await call_park.park(Session(), Bus(), object(), SETTINGS, call, user=AGENT)
    assert "parked" not in call.extra


@pytest.mark.asyncio
async def test_parked_events_reach_only_members_of_the_number():
    org = uuid.uuid4()
    member = InboxAccess(
        is_admin=False, member_e164s=frozenset({"+14694617145"}), viewer_e164s=frozenset()
    )
    viewer = InboxAccess(
        is_admin=False, member_e164s=frozenset(), viewer_e164s=frozenset({"+14694617145"})
    )
    event = {"type": "call.parked", "to": "+14694617145", "state": "parked"}
    assert await _event_visible(event, member, org, MATE.id) is True
    assert await _event_visible(event, viewer, org, MATE.id) is False
    assert await _event_visible({"type": "call.parked"}, member, org, MATE.id) is False


async def test_list_parked_filters_live_parked_calls_by_number_access(session):
    from datetime import datetime, timezone

    from app.db.base import set_org_context
    from app.models import Call, Org

    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Park", slug=f"park-{org_id.hex[:8]}"))
    await session.commit()
    set_org_context(session, org_id)
    parked = {"parked": {"by_name": "Ana Agent", "at": datetime.now(timezone.utc).isoformat()}}

    def mk(our, status, extra):
        call = Call(
            id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164="+14695550100",
            our_e164=our, carrier="livekit", status=status, extra=extra,
        )
        session.add(call)
        return call

    live = mk("+14694617145", "answered", {"via": "livekit", "room": "r1", **parked})
    mk("+14694617145", "completed", {"via": "livekit", "room": "r2", **parked})  # ended
    mk("+14694617145", "answered", {"via": "livekit", "room": "r3"})  # not parked
    mk("+19995550000", "answered", {"via": "livekit", "room": "r4", **parked})  # other number
    await session.commit()

    access = InboxAccess(
        is_admin=False, member_e164s=frozenset({"+14694617145"}), viewer_e164s=frozenset()
    )
    rows = await call_park.list_parked(session, access)
    assert [r["call_id"] for r in rows] == [str(live.id)]
    assert rows[0]["by"] == "Ana Agent"
