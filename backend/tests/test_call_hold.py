"""Hold with music: who is muted from whom, the music worker, resume, and refusals."""

from __future__ import annotations

import types

import pytest

from app.errors import ConflictError, FeatureUnavailableError
from app.services import call_hold


class FakeApi:
    def __init__(self, participants):
        self.participants = participants
        self.subs: list[tuple[str, tuple, bool]] = []
        self.dispatched: list[dict] = []
        self.removed: list[str] = []

    async def list_participants(self, room):
        return self.participants

    async def update_subscriptions(self, *, room, identity, track_sids, subscribe):
        self.subs.append((identity, tuple(track_sids), subscribe))

    async def create_agent_dispatch(self, *, room, agent_name, metadata):
        self.dispatched.append({"room": room, "agent_name": agent_name, "metadata": metadata})

    async def remove_participant(self, room, identity):
        self.removed.append(identity)


PHONE = {"identity": "sip-1", "kind": "SIP", "attributes": {"sip.callID": "abc"},
         "tracks": [{"sid": "TR_phone", "type": "AUDIO"}]}
PERSON = {"identity": "user-1", "kind": "STANDARD", "attributes": {},
          "tracks": [{"sid": "TR_person", "type": "AUDIO"}]}
RECORDER = {"identity": "lkrec", "kind": "AGENT", "attributes": {}, "tracks": []}
MUSIC = {"identity": "agent-hold", "kind": "AGENT", "attributes": {"csaas.role": "hold-music"},
         "tracks": [{"sid": "TR_music", "type": "AUDIO"}]}


class Session:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _call(**extra):
    return types.SimpleNamespace(
        id="call-1", status="answered", extra={"via": "livekit", "room": "call-1", **extra}
    )


SETTINGS = types.SimpleNamespace(hold_music_agent_name="hold-music")


@pytest.mark.asyncio
async def test_hold_mutes_both_directions_and_dispatches_music():
    api = FakeApi([PHONE, PERSON, RECORDER])
    call = _call()
    session = Session()

    await call_hold.set_hold(session, api, SETTINGS, call, on=True)

    assert ("sip-1", ("TR_person",), False) in api.subs
    assert ("user-1", ("TR_phone",), False) in api.subs
    assert not any(identity == "lkrec" for identity, _, _ in api.subs)
    assert api.dispatched[0]["agent_name"] == "hold-music"
    assert call.extra["on_hold"]["since"]
    assert session.commits == 1

    # Pressing Hold again does nothing.
    await call_hold.set_hold(session, api, SETTINGS, call, on=True)
    assert len(api.dispatched) == 1


@pytest.mark.asyncio
async def test_resume_removes_music_and_resubscribes():
    api = FakeApi([PHONE, PERSON, MUSIC])
    call = _call(on_hold={"since": "x"})

    await call_hold.set_hold(Session(), api, SETTINGS, call, on=False)

    assert api.removed == ["agent-hold"]
    assert ("sip-1", ("TR_person",), True) in api.subs
    assert ("user-1", ("TR_phone",), True) in api.subs
    assert "on_hold" not in call.extra


@pytest.mark.asyncio
async def test_hold_refused_for_ended_carrier_or_unconfigured():
    api = FakeApi([PHONE, PERSON])
    ended = _call()
    ended.status = "completed"
    with pytest.raises(ConflictError):
        await call_hold.set_hold(Session(), api, SETTINGS, ended, on=True)

    carrier = types.SimpleNamespace(id="c", status="answered", extra={"via": "carrier"})
    with pytest.raises(ConflictError):
        await call_hold.set_hold(Session(), api, SETTINGS, carrier, on=True)

    with pytest.raises(FeatureUnavailableError):
        await call_hold.set_hold(
            Session(), api, types.SimpleNamespace(hold_music_agent_name=""), _call(), on=True
        )
