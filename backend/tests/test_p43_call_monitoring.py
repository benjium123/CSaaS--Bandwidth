# ruff: noqa: E501
"""P43 call monitoring: which calls, recording + announcement, transcripts, AI review,
behaviour signals, and the silent listener for softphone calls."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa

from app.api.routes.webhooks import _outbound_answer_commands
from app.db.base import set_org_context
from app.main import create_app
from app.models import (
    Call,
    CallLeg,
    CallRecording,
    CallReview,
    CallTranscriptSegment,
    Message,
    MessageThread,
    MonitorSignal,
    Org,
    OrgMonitoring,
)
from app.providers.voice import Speak, StartRecording
from app.services import monitor_calls, monitor_score
from tests.conftest import (
    WEBHOOK_PASS,
    WEBHOOK_USER,
    auth_headers,
    make_org_with_number,
    make_settings,
)
from tests.fake_ai import FakeSafetyAI
from tests.test_voice_webhooks import FakeVoiceCarrier, install_voice_carrier

OUR = "+15125550100"
THEM = "+15125550199"


@pytest.fixture
def mon_settings():
    return make_settings(
        monitor_enforced=True,
        deepgram_api_key="dg-test",
        livekit_api_key="lk-test-key",
        livekit_api_secret="lk-test-secret-value-padded-to-32-bytes-plus",
        bandwidth_webhook_username=WEBHOOK_USER,
        bandwidth_webhook_password=WEBHOOK_PASS,
    )


@pytest.fixture
async def voice(engine, mon_settings):
    application = create_app(mon_settings)
    carrier = FakeVoiceCarrier()
    install_voice_carrier(application, carrier)
    fake = FakeSafetyAI()
    with fake.installed():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            yield client, carrier, fake, application


async def _org(session, *, age_days: int = 0) -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(
        Org(
            id=org_id,
            name="Mon",
            slug=f"mon-{org_id.hex[:8]}",
            created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        )
    )
    await session.commit()
    return org_id


# --- which calls ---------------------------------------------------------------------------


async def test_choose_new_watch_sample_and_off(session, mon_settings):
    new_org = await _org(session)
    old_org = await _org(session, age_days=400)
    assert await monitor_calls.choose(session, mon_settings, new_org) == "new_account"
    assert await monitor_calls.choose(session, mon_settings, old_org, rng=lambda: 0.05) == "sample"
    assert await monitor_calls.choose(session, mon_settings, old_org, rng=lambda: 0.9) is None
    set_org_context(session, old_org)
    session.add(OrgMonitoring(id=uuid.uuid4(), org_id=old_org, score=40, level="watch"))
    await session.commit()
    assert await monitor_calls.choose(session, mon_settings, old_org, rng=lambda: 0.9) == "watch"
    assert await monitor_calls.choose(session, make_settings(), new_org) is None


def test_monitored_calls_always_announce_before_recording():
    org = SimpleNamespace(recording_announcement=False, recording_announcement_text="")
    monitored = SimpleNamespace(extra={"monitor": "new_account", "record": True})
    commands = _outbound_answer_commands(monitored, org, needs_pause=False)
    assert isinstance(commands[0], Speak) and isinstance(commands[1], StartRecording)
    plain = SimpleNamespace(extra={})
    assert _outbound_answer_commands(plain, org, needs_pause=False) == []


async def test_outbound_call_from_new_account_is_recorded_and_queued(voice, session):
    client, carrier, _fake, _app = voice
    token, org, _num = await make_org_with_number(client, f"c-{uuid.uuid4().hex[:6]}@example.com", "Caller", OUR)
    r = await client.post(
        "/api/v1/calls", json={"to": THEM, "from": OUR}, headers=auth_headers(token, org["id"])
    )
    assert r.status_code in (200, 201), r.text
    set_org_context(session, uuid.UUID(org["id"]))
    call = (await session.execute(sa.select(Call))).scalar_one()
    assert call.extra["monitor"] == "new_account" and call.extra["record"] is True
    review = (await session.execute(sa.select(CallReview))).scalar_one()
    assert review.call_id == call.id and review.status == "pending"


# --- review -------------------------------------------------------------------------------


async def _finished_call(session, org_id, *, duration=120, minutes_ago=10, direction="outbound") -> Call:
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction=direction,
        contact_e164=THEM,
        our_e164=OUR,
        carrier="bandwidth",
        status="completed",
        duration_seconds=duration,
        ended_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        extra={"monitor": "new_account", "record": True},
    )
    session.add(call)
    await session.flush()
    session.add(CallReview(id=uuid.uuid4(), org_id=org_id, call_id=call.id, reason="new_account"))
    await session.commit()
    return call


async def test_scam_transcript_becomes_a_signal(voice, session, mon_settings):
    _client, _carrier, fake, app = voice
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    set_org_context(session, org_id)
    for i, (role, text) in enumerate(
        [("agent", "This is the IRS, there is a warrant for your arrest."), ("user", "What?"),
         ("agent", "Pay with Apple gift cards and read me the numbers.")]
    ):
        session.add(CallTranscriptSegment(id=uuid.uuid4(), org_id=org_id, call_id=call.id, role=role, text=text, at_ms=i * 1000))
    await session.commit()
    fake.call_verdict = lambda t: {
        "verdict": "scam", "confidence": 95, "category": "impersonation",
        "summary": "IRS impersonation demanding gift cards.",
        "evidence": [{"speaker": "agent", "quote": "Pay with Apple gift cards"}],
    }
    counts = await monitor_calls.review_tick(session, mon_settings, app.state.media_store)
    assert counts.get("scam") == 1
    set_org_context(session, org_id)
    review = (await session.execute(sa.select(CallReview))).scalar_one()
    assert review.status == "reviewed" and review.verdict == "scam"
    assert "gift cards" in review.evidence[0]["quote"]
    signal = (await session.execute(sa.select(MonitorSignal))).scalar_one()
    assert signal.kind == "call_scam" and signal.call_id == call.id
    # the transcript itself is the business's words - the AI saw it as data
    assert "<data" in fake.tasks("call transcript")[0]["messages"][1]["content"]


async def test_recording_is_transcribed_with_speakers_then_reviewed(voice, session, mon_settings, monkeypatch):
    _client, _carrier, fake, app = voice
    org_id = await _org(session)
    call = await _finished_call(session, org_id)
    set_org_context(session, org_id)
    recording = CallRecording(
        id=uuid.uuid4(), org_id=org_id, call_id=call.id, provider_recording_id="r1",
        storage_key=f"org/{org_id}/rec", content_type="audio/mpeg", status="stored",
    )
    session.add(recording)
    await session.commit()

    class Store:
        async def get(self, key):
            return b"ID3fakeaudio"

    deepgram_payload = {
        "results": {
            "utterances": [
                {"speaker": 0, "transcript": "Hello?", "start": 0.4},
                {"speaker": 1, "transcript": "Hi, this is Acme Plumbing about tomorrow.", "start": 1.5},
            ]
        }
    }
    seen = {}

    async def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=deepgram_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as dg:
        counts = await monitor_calls.review_tick(session, mon_settings, Store(), client=dg)
    assert counts.get("ok") == 1
    assert seen["auth"] == "Token dg-test" and seen["params"]["diarize"] == "true"
    set_org_context(session, org_id)
    segments = (await session.execute(sa.select(CallTranscriptSegment).order_by(CallTranscriptSegment.at_ms))).scalars().all()
    assert [(s.role, s.text) for s in segments] == [
        ("user", "Hello?"),
        ("agent", "Hi, this is Acme Plumbing about tomorrow."),
    ]


async def test_short_or_unfinished_calls_are_skipped_or_wait(voice, session, mon_settings):
    _client, _carrier, _fake, app = voice
    org_id = await _org(session)
    await _finished_call(session, org_id, duration=5)
    await _finished_call(session, org_id, minutes_ago=0)
    counts = await monitor_calls.review_tick(session, mon_settings, app.state.media_store)
    assert counts == {"skipped": 1, "waiting": 1}


# --- behaviour ------------------------------------------------------------------------------


async def test_short_call_pattern_and_angry_replies_become_signals(voice, session, mon_settings):
    org_id = await _org(session)
    set_org_context(session, org_id)
    for _ in range(40):
        session.add(
            Call(id=uuid.uuid4(), org_id=org_id, direction="outbound", contact_e164=THEM, our_e164=OUR,
                 carrier="bandwidth", status="completed", duration_seconds=4)
        )
    thread = MessageThread(id=uuid.uuid4(), org_id=org_id, our_e164=OUR, contact_e164=THEM)
    session.add(thread)
    await session.flush()
    session.add(
        Message(id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="inbound", status="received",
                from_e164=THEM, to_e164=OUR, body="Who is this? Stop texting me, scammer")
    )
    await session.commit()
    counts = await monitor_calls.behaviour_tick(session, mon_settings)
    assert counts["signals"] == 2
    set_org_context(session, org_id)
    kinds = sorted(s.kind for s in (await session.execute(sa.select(MonitorSignal))).scalars().all())
    assert kinds == ["complaint_reply", "short_calls"]
    # once a day per kind: running again adds nothing
    assert (await monitor_calls.behaviour_tick(session, mon_settings))["signals"] == 0


# --- softphone rooms -------------------------------------------------------------------------


class FakeLiveKit:
    def __init__(self):
        self.dispatches = []

    async def create_agent_dispatch(self, *, room, agent_name, metadata=""):
        self.dispatches.append({"room": room, "agent_name": agent_name, "metadata": json.loads(metadata)})
        return {}


async def test_listener_joins_monitored_inbound_room_calls(voice, session, mon_settings):
    org_id = await _org(session)
    set_org_context(session, org_id)
    org = await session.get(Org, org_id)
    org.recording_announcement_text = "This call may be recorded for quality and safety."
    call = Call(id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164=THEM, our_e164=OUR,
                carrier="telnyx", status="initiated", extra={"via": "livekit", "room": "inbound-room-1"})
    session.add(call)
    await session.flush()
    session.add(CallLeg(id=uuid.uuid4(), org_id=org_id, call_id=call.id, provider_call_id="SIP-123",
                        to_e164=OUR, from_e164=THEM, status="ringing", reason="original"))
    await session.commit()
    api = FakeLiveKit()
    event = {
        "event": "participant_joined",
        "room": {"name": "inbound-room-1"},
        "participant": {"identity": "sip-x", "attributes": {"sip.callID": "SIP-123"}},
    }
    await monitor_calls.on_livekit_event(session, api, mon_settings, event)
    assert len(api.dispatches) == 1
    dispatch = api.dispatches[0]
    assert dispatch["agent_name"] == "call-monitor"
    assert dispatch["metadata"]["call_id"] == str(call.id)
    assert dispatch["metadata"]["announcement"].startswith("This call may be recorded")
    set_org_context(session, org_id)
    await session.refresh(call)
    assert call.extra["monitor"] == "new_account" and call.extra["monitor_dispatched"] is True
    # redelivered webhook: no second listener
    await monitor_calls.on_livekit_event(session, api, mon_settings, event)
    assert len(api.dispatches) == 1


async def test_pause_blocks_calls_too(voice, session, mon_settings):
    client, _carrier, _fake, _app = voice
    token, org, _num = await make_org_with_number(client, f"p-{uuid.uuid4().hex[:6]}@example.com", "Paused", OUR)
    org_id = uuid.UUID(org["id"])
    for _ in range(3):
        await monitor_score.add_signal(session, mon_settings, org_id, "call_scam", "Scam call")
    await session.commit()
    r = await client.post("/api/v1/calls", json={"to": THEM, "from": OUR}, headers=auth_headers(token, org["id"]))
    assert r.status_code == 403 and r.json()["error"]["code"] == "account_paused"
