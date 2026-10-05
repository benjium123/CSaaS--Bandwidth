"""Teammate invites on live softphone calls: add / transfer, join, cancel, privacy, and the
rule that transfers never leave the workspace."""

from __future__ import annotations

import types
import uuid

import pytest

from app.api.routes.softphone import _event_visible
from app.errors import ConflictError, ValidationFailedError
from app.services import call_hold, call_invites
from app.services.inbox_access import InboxAccess

INVITER = types.SimpleNamespace(id=uuid.uuid4(), full_name="Ana Agent", email="ana@x.test")
MATE = types.SimpleNamespace(id=uuid.uuid4(), full_name="Sam Mate", email="sam@x.test")
OTHER = types.SimpleNamespace(id=uuid.uuid4(), full_name="Out Sider", email="out@x.test")
SETTINGS = types.SimpleNamespace(hold_music_agent_name="hold-music")


class Bus:
    def __init__(self):
        self.events: list[dict] = []

    def publish(self, org_id, event):
        self.events.append(event)


class Session:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _call(**extra):
    return types.SimpleNamespace(
        id=uuid.uuid4(), org_id=uuid.uuid4(), status="answered",
        contact_e164="+14695550100", our_e164="+14694617145",
        extra={"via": "livekit", "room": "call-1", **extra},
    )


@pytest.fixture
def world(monkeypatch):
    holds: list[bool] = []

    async def teammates_for(session, call, *, exclude_user_id):
        return [{"user_id": str(MATE.id), "name": "Sam Mate", "email": MATE.email}]

    async def set_hold(session, api, settings, call, *, on):
        holds.append(on)
        extra = dict(call.extra or {})
        if on:
            extra["on_hold"] = {"since": "now"}
        else:
            extra.pop("on_hold", None)
        call.extra = extra

    monkeypatch.setattr(call_invites, "teammates_for", teammates_for)
    monkeypatch.setattr(call_hold, "set_hold", set_hold)
    return holds


@pytest.mark.asyncio
async def test_transfer_invite_holds_caller_rings_only_the_teammate_and_join_resumes(world):
    bus, call = Bus(), _call()
    await call_invites.invite(
        Session(),
        bus,
        object(),
        SETTINGS,
        call,
        inviter=INVITER,
        target_user_id=MATE.id,
        mode="transfer",
    )
    assert world == [True]  # caller hears hold music while it rings
    ring = bus.events[-1]
    assert ring["type"] == "call.invite" and ring["user_id"] == str(MATE.id)
    assert ring["mode"] == "transfer" and ring["by"] == "Ana Agent"

    room = await call_invites.accept(Session(), bus, object(), SETTINGS, call, user=MATE)
    assert room == "call-1"
    assert world == [True, False]  # music off once the teammate is on
    update = bus.events[-1]
    assert update == {
        "type": "call.invite.update", "user_id": str(INVITER.id), "call_id": str(call.id),
        "invitee": str(
            MATE.id
        ), "invitee_name": "Sam Mate", "mode": "transfer", "state": "accepted",
    }
    # The invite is single-use.
    with pytest.raises(ConflictError):
        await call_invites.accept(Session(), bus, object(), SETTINGS, call, user=MATE)


@pytest.mark.asyncio
async def test_add_invite_does_not_hold_and_strangers_cannot_join(world):
    bus, call = Bus(), _call()
    await call_invites.invite(
        Session(),
        bus,
        object(),
        SETTINGS,
        call,
        inviter=INVITER,
        target_user_id=MATE.id,
        mode="add",
    )
    assert world == []
    with pytest.raises(ConflictError):
        await call_invites.accept(Session(), bus, object(), SETTINGS, call, user=OTHER)


@pytest.mark.asyncio
async def test_only_teammates_on_this_number_can_be_invited(world):
    call = _call()
    with pytest.raises(ValidationFailedError):
        await call_invites.invite(
            Session(),
            Bus(),
            object(),
            SETTINGS,
            call,
            inviter=INVITER,
            target_user_id=OTHER.id,
            mode="add",
        )
    with pytest.raises(ValidationFailedError):
        await call_invites.invite(
            Session(),
            Bus(),
            object(),
            SETTINGS,
            call,
            inviter=INVITER,
            target_user_id=INVITER.id,
            mode="add",
        )
    ended = _call()
    ended.status = "completed"
    with pytest.raises(ConflictError):
        await call_invites.invite(
            Session(),
            Bus(),
            object(),
            SETTINGS,
            ended,
            inviter=INVITER,
            target_user_id=MATE.id,
            mode="add",
        )


@pytest.mark.asyncio
async def test_decline_resumes_the_caller_and_tells_the_inviter(world):
    bus, call = Bus(), _call()
    await call_invites.invite(
        Session(),
        bus,
        object(),
        SETTINGS,
        call,
        inviter=INVITER,
        target_user_id=MATE.id,
        mode="transfer",
    )
    await call_invites.cancel(Session(), bus, object(), SETTINGS, call, actor=MATE, user_id=MATE.id)
    assert world == [True, False]
    assert bus.events[-1]["state"] == "declined" and bus.events[-1]["user_id"] == str(INVITER.id)
    with pytest.raises(ConflictError):
        await call_invites.accept(Session(), bus, object(), SETTINGS, call, user=MATE)


@pytest.mark.asyncio
async def test_expired_invites_cannot_be_used(world, monkeypatch):
    from datetime import datetime, timedelta, timezone

    bus, call = Bus(), _call()
    await call_invites.invite(
        Session(),
        bus,
        object(),
        SETTINGS,
        call,
        inviter=INVITER,
        target_user_id=MATE.id,
        mode="add",
    )
    later = datetime.now(timezone.utc) + call_invites.INVITE_TTL + timedelta(seconds=1)
    monkeypatch.setattr(call_invites, "_now", lambda: later)
    with pytest.raises(ConflictError):
        await call_invites.accept(Session(), bus, object(), SETTINGS, call, user=MATE)


@pytest.mark.asyncio
async def test_invite_events_reach_only_the_named_person_not_admins():
    admin = InboxAccess(is_admin=True, member_e164s=frozenset(), viewer_e164s=frozenset())
    org = uuid.uuid4()
    for kind in ("call.invite", "call.invite.update"):
        event = {"type": kind, "user_id": str(MATE.id), "to": "+14694617145"}
        assert await _event_visible(event, admin, org, MATE.id) is True
        assert await _event_visible(event, admin, org, INVITER.id) is False
        assert await _event_visible({"type": kind}, admin, org, MATE.id) is False
