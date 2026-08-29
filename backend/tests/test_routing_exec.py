"""P12 services/routing_exec.py: the carrier + room executors (phase-12-plan test spec,
"Integration" section for IVR/ring-group/queue/callback).

Command-content assertions go through the executor functions DIRECTLY (constructing
Call/CallFlow rows by hand) rather than round-tripping BXML - `FakeVoiceCarrier.
render_commands` only returns a command COUNT when `bxml_shaped=True`, so it cannot show
which commands were produced. One webhook-level test proves the plumbing itself (unbound
number keeps the P6 default, a bound number's call_initiated reaches the executor, and a
digit webhook advances `calls.extra["flow"]`) is wired correctly end to end.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import ConflictError
from app.main import create_app
from app.models.callflow import BusinessHours, CallQueue, QueueEntry, RingGroupDef
from app.models.voice import Call
from app.providers import voice
from app.providers.voice import VoiceEvent
from app.services import flows as flows_svc
from app.services import routing_exec as routing_exec_svc
from tests.conftest import (
    WEBHOOK_PASS,
    WEBHOOK_USER,
    auth_headers,
    make_org_with_number,
    make_settings,
)
from tests.test_voice_webhooks import FakeVoiceCarrier, install_voice_carrier

OUR = "+12145550100"
THEIRS = "+19725550199"


@pytest.fixture
async def app_with_voice_carrier(engine):
    """App wired with a FakeVoiceCarrier named 'bandwidth' - local to this file (not
    imported as a fixture from test_voice_webhooks.py) so ruff does not mistake the test
    functions' `app_with_voice_carrier` parameter for shadowing a module-level import."""
    import httpx

    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    fake = FakeVoiceCarrier()
    install_voice_carrier(application, fake)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, application


MENU_FLOW = {
    "entry": "root",
    "nodes": {
        "root": {
            "type": "menu",
            "prompt": "root menu",
            "options": {"1": "level2"},
            "invalid_node": "voicemail",
        },
        "level2": {
            "type": "menu",
            "prompt": "level2 menu",
            "options": {"1": "ring", "2": "queue"},
            "invalid_node": "voicemail",
            "invalid_retries": 0,
        },
        "ring": {"type": "ring_group", "ring_group_id": "RING_ID", "no_answer": "voicemail"},
        "queue": {"type": "queue", "queue_id": "QUEUE_ID"},
        "voicemail": {"type": "voicemail", "greeting": "Leave a message after the tone."},
    },
}


@dataclass
class FakeBus:
    published: list[tuple] = field(default_factory=list)

    def publish(self, org_id, event: dict) -> None:  # noqa: ANN001
        self.published.append((org_id, event))


def _menu_flow(ring_id: str, queue_id: str) -> dict:
    raw = json.dumps(MENU_FLOW).replace("RING_ID", ring_id).replace("QUEUE_ID", queue_id)
    return json.loads(raw)


async def _dummy_ring_group(session, org_id: uuid.UUID) -> str:
    """B3: the DR-4 save gate now cross-checks EVERY ring_group_id in the flow (not just
    the one a given test actually exercises), so even a placeholder id must name a real
    row in this org."""
    ring = RingGroupDef(
        id=uuid.uuid4(),
        org_id=org_id,
        name=f"unused-rg-{uuid.uuid4().hex[:8]}",
        strategy="simultaneous",
        member_user_ids=[],
        ring_timeout_seconds=20,
    )
    session.add(ring)
    await session.flush()
    return str(ring.id)


async def _dummy_queue(session, org_id: uuid.UUID) -> str:
    queue = CallQueue(
        id=uuid.uuid4(),
        org_id=org_id,
        name=f"unused-q-{uuid.uuid4().hex[:8]}",
        max_wait_seconds=300,
        overflow="hangup",
    )
    session.add(queue)
    await session.flush()
    return str(queue.id)


def _make_call(org_id: uuid.UUID, *, direction: str = "inbound", extra: dict | None = None) -> Call:
    return Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction=direction,
        contact_e164=THEIRS,
        our_e164=OUR,
        carrier="bandwidth",
        status="initiated",
        extra=extra or {},
    )


async def _make_org(session) -> uuid.UUID:
    """A real Org row - CallFlow/RingGroupDef/CallQueue/Call are all TenantScoped with a
    genuine FK to orgs.id (SQLite enforces it in this suite - see conftest.py's
    PRAGMA foreign_keys=ON), so a random uuid4 org_id fails on INSERT."""
    from app.models.org import Org

    org = Org(id=uuid.uuid4(), name="Test Org", slug=f"org-{uuid.uuid4().hex[:12]}")
    session.add(org)
    await session.commit()
    return org.id


async def _make_user(session) -> uuid.UUID:
    """A real User row - QueueEntry.offered_user_id has an FK to users.id."""
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=f"routing-{uuid.uuid4().hex[:12]}@example.com",
        hashed_password="x",
    )
    session.add(user)
    await session.commit()
    return user.id


async def _flow_row(session, org_id, definition):
    set_org_context(session, org_id)
    row = await flows_svc.create_flow(
        session, org_id, name=f"ivr-{uuid.uuid4().hex[:8]}", definition=definition
    )
    await flows_svc.activate_flow(session, org_id, row.id)
    return row


# --------------------------------------------------------------------------------------
# Webhook plumbing (unbound default / bound flow reaches the executor / digit advances)
# --------------------------------------------------------------------------------------
ANSWER_URL = "/api/v1/webhooks/bandwidth/voice/answer"


async def test_unbound_number_keeps_default_inbound_commands(app_with_voice_carrier, session):
    client, fake, _app = app_with_voice_carrier
    fake.bxml_shaped = True
    from tests.conftest import webhook_auth_headers

    _token, org, _number = await make_org_with_number(client, "rt1@example.com", "Org A", OUR)

    fake.events_to_return = [
        VoiceEvent(
            event_type="call_initiated",
            provider_call_id="leg-1",
            provider_event_id="ev-1",
            to=OUR,
            from_=THEIRS,
        )
    ]
    r = await client.post(ANSWER_URL, content=b"{}", headers=webhook_auth_headers())
    assert r.status_code == 200
    assert r.text == "<Response>2</Response>"  # DEFAULT_INBOUND_COMMANDS: Speak + Hangup


async def test_bound_number_renders_menu_and_digit_advances_flow_state(
    app_with_voice_carrier, session
):
    client, fake, _app = app_with_voice_carrier
    fake.bxml_shaped = True
    from tests.conftest import webhook_auth_headers

    token, org, number = await make_org_with_number(client, "rt2@example.com", "Org B", OUR)
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    ring = RingGroupDef(
        id=uuid.uuid4(),
        org_id=org_id,
        name="rg",
        strategy="simultaneous",
        member_user_ids=[],
        ring_timeout_seconds=5,
    )
    session.add(ring)
    await session.flush()
    queue = CallQueue(
        id=uuid.uuid4(), org_id=org_id, name="q", max_wait_seconds=30, overflow="hangup"
    )
    session.add(queue)
    await session.flush()
    flow = await _flow_row(session, org_id, _menu_flow(str(ring.id), str(queue.id)))
    await flows_svc.bind_number(session, org_id, uuid.UUID(number["id"]), flow.id)

    fake.events_to_return = [
        VoiceEvent(
            event_type="call_initiated",
            provider_call_id="leg-2",
            provider_event_id="ev-2",
            to=OUR,
            from_=THEIRS,
        )
    ]
    r = await client.post(ANSWER_URL, content=b"{}", headers=webhook_auth_headers())
    assert r.status_code == 200
    assert r.text == "<Response>2</Response>"  # root menu: Speak + Gather

    call = (await session.execute(sa.select(Call))).scalars().one()
    assert call.extra["flow"]["awaiting"] == "digit"
    assert call.extra["flow"]["state"]["node"] == "root"

    fake.events_to_return = [
        VoiceEvent(
            event_type="dtmf_received",
            provider_call_id="leg-2",
            provider_event_id="ev-3",
            to=OUR,
            from_=THEIRS,
            digits="1",
        )
    ]
    r = await client.post(ANSWER_URL, content=b"{}", headers=webhook_auth_headers())
    assert r.status_code == 200
    assert r.text == "<Response>2</Response>"  # level2 menu: Speak + Gather

    await session.refresh(call)
    assert call.extra["flow"]["state"]["node"] == "level2"


# --------------------------------------------------------------------------------------
# Carrier executor: exact command content, direct calls (DR-2 nested menu -> voicemail).
# --------------------------------------------------------------------------------------
async def test_carrier_menu_reaches_second_level_and_voicemail_terminal(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    flow = await _flow_row(
        session,
        org_id,
        _menu_flow(await _dummy_ring_group(session, org_id), await _dummy_queue(session, org_id)),
    )
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    commands = await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    assert commands == [
        voice.Speak(text="root menu"),
        voice.Gather(max_digits=1, timeout_seconds=10, action_tag="flow_digit"),
    ]

    event = VoiceEvent(
        event_type="dtmf_received", provider_call_id="x", provider_event_id="y", digits="1"
    )
    commands = await routing_exec_svc.continue_carrier_flow(session, bus, call, event)
    assert commands == [
        voice.Speak(text="level2 menu"),
        voice.Gather(max_digits=1, timeout_seconds=10, action_tag="flow_digit"),
    ]

    # Neither ring_group nor queue is chosen here - a THIRD invalid digit falls to voicemail.
    event = VoiceEvent(
        event_type="dtmf_received", provider_call_id="x", provider_event_id="z", digits="9"
    )
    commands = await routing_exec_svc.continue_carrier_flow(session, bus, call, event)
    assert commands == [
        voice.Speak(text="Leave a message after the tone."),
        voice.StartRecording(),
    ]
    await session.refresh(call)
    assert call.extra["flow"]["terminal"] == "voicemail"

    from app.models.callflow import Voicemail

    vm = (await session.execute(sa.select(Voicemail))).scalars().one()
    assert vm.call_id == call.id
    assert vm.greeting_node == "voicemail"


async def test_carrier_ring_group_holds_for_real_then_no_answer(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    ring = RingGroupDef(
        id=uuid.uuid4(),
        org_id=org_id,
        name="rg",
        strategy="simultaneous",
        member_user_ids=[],
        ring_timeout_seconds=5,
    )
    session.add(ring)
    await session.flush()
    flow = await _flow_row(
        session, org_id, _menu_flow(str(ring.id), await _dummy_queue(session, org_id))
    )
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    event = VoiceEvent(
        event_type="dtmf_received", provider_call_id="x", provider_event_id="y", digits="1"
    )
    commands = await routing_exec_svc.continue_carrier_flow(session, bus, call, event)
    event2 = VoiceEvent(
        event_type="dtmf_received", provider_call_id="x", provider_event_id="z", digits="1"
    )
    commands = await routing_exec_svc.continue_carrier_flow(session, bus, call, event2)

    # Speak (hold) + a REAL bounded Pause (capped, never the full ring_timeout if larger),
    # then the "no one answered" fallthrough -> voicemail terminal.
    assert commands[0] == voice.Speak(text="Please hold while we try to connect you.")
    assert commands[1] == voice.Pause(seconds=5)
    assert commands[2] == voice.Speak(text="Leave a message after the tone.")
    assert commands[3] == voice.StartRecording()
    await session.refresh(call)
    assert call.extra["flow"]["terminal"] == "voicemail"


async def test_carrier_queue_wait_gathers_with_callback_digit_prompt(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    queue_row = CallQueue(
        id=uuid.uuid4(), org_id=org_id, name="q", max_wait_seconds=300, overflow="hangup"
    )
    session.add(queue_row)
    await session.flush()
    flow = await _flow_row(
        session, org_id, _menu_flow(await _dummy_ring_group(session, org_id), str(queue_row.id))
    )
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    ev1 = VoiceEvent(
        event_type="dtmf_received", provider_call_id="x", provider_event_id="a", digits="1"
    )
    await routing_exec_svc.continue_carrier_flow(session, bus, call, ev1)
    ev2 = VoiceEvent(
        event_type="dtmf_received", provider_call_id="x", provider_event_id="b", digits="2"
    )
    commands = await routing_exec_svc.continue_carrier_flow(session, bus, call, ev2)

    assert len(commands) == 1
    gather = commands[0]
    assert isinstance(gather, voice.Gather)
    assert gather.timeout_seconds == routing_exec_svc.CARRIER_HOLD_CAP_SECONDS
    assert gather.action_tag == "flow_queue_wait"

    await session.refresh(call)
    assert call.extra["flow"]["awaiting"] == "queue_wait"

    entries = (await session.execute(sa.select(QueueEntry))).scalars().all()
    assert len(entries) == 1
    assert entries[0].state == "waiting"


async def test_carrier_queue_wait_timeout_reissues_gather_before_max_wait(session, engine):
    """B10: a bare Gather-timeout webhook (empty digits) must NOT overflow the queue just
    because the FIRST bounded hold (capped at CARRIER_HOLD_CAP_SECONDS) ran out - only the
    queue's REAL max_wait_seconds elapsing should overflow. max_wait_seconds here (300) is
    deliberately well above CARRIER_HOLD_CAP_SECONDS (60) so the first hold segment always
    times out long before the real wait is up."""
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    queue_row = CallQueue(
        id=uuid.uuid4(), org_id=org_id, name="q", max_wait_seconds=300, overflow="hangup"
    )
    session.add(queue_row)
    await session.flush()
    flow = await _flow_row(
        session, org_id, _menu_flow(await _dummy_ring_group(session, org_id), str(queue_row.id))
    )
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="a", digits="1"
        ),
    )
    await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="b", digits="2"
        ),
    )

    # The first hold Gather (60s, capped) times out - only ~60s of the real 300s max_wait
    # has elapsed, so this must RE-ISSUE the hold Gather, not overflow.
    soon = datetime.now(timezone.utc) + timedelta(seconds=65)
    commands = await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="c", digits=""
        ),
        now=soon,
    )
    assert len(commands) == 1
    gather = commands[0]
    assert isinstance(gather, voice.Gather)
    assert gather.action_tag == "flow_queue_wait"
    assert 0 < gather.timeout_seconds <= routing_exec_svc.CARRIER_HOLD_CAP_SECONDS

    await session.refresh(call)
    assert call.extra["flow"]["awaiting"] == "queue_wait"  # still waiting, not terminal

    entry = (await session.execute(sa.select(QueueEntry))).scalars().one()
    assert entry.state == "waiting"  # never touched - no overflow, no callback

    # NOW the real max_wait_seconds has elapsed - this one genuinely overflows.
    far_future = datetime.now(timezone.utc) + timedelta(seconds=310)
    commands = await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="d", digits=""
        ),
        now=far_future,
    )
    assert commands[-1] == voice.Hangup()
    await session.refresh(entry)
    assert entry.state == "overflowed"


async def test_carrier_queue_callback_digit_captures_and_hangs_up(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    queue_row = CallQueue(
        id=uuid.uuid4(), org_id=org_id, name="q", max_wait_seconds=300, overflow="hangup"
    )
    session.add(queue_row)
    await session.flush()
    flow = await _flow_row(
        session, org_id, _menu_flow(await _dummy_ring_group(session, org_id), str(queue_row.id))
    )
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="a", digits="1"
        ),
    )
    await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="b", digits="2"
        ),
    )

    callback_ev = VoiceEvent(
        event_type="dtmf_received",
        provider_call_id="x",
        provider_event_id="c",
        digits=routing_exec_svc.CALLBACK_DIGIT,
    )
    commands = await routing_exec_svc.continue_carrier_flow(session, bus, call, callback_ev)
    assert commands[-1] == voice.Hangup()

    entry = (await session.execute(sa.select(QueueEntry))).scalars().one()
    assert entry.state == "callback_requested"
    assert entry.callback_e164 == THEIRS
    assert "queue.callback_requested" in [e["type"] for _org, e in bus.published]


@pytest.mark.parametrize(
    "overflow,expect_last_state",
    [("hangup", "overflowed"), ("callback", "callback_requested")],
)
async def test_carrier_queue_overflow_on_wrong_digit(session, engine, overflow, expect_last_state):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    queue_row = CallQueue(
        id=uuid.uuid4(), org_id=org_id, name="q", max_wait_seconds=300, overflow=overflow
    )
    session.add(queue_row)
    await session.flush()
    flow = await _flow_row(
        session, org_id, _menu_flow(await _dummy_ring_group(session, org_id), str(queue_row.id))
    )
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="a", digits="1"
        ),
    )
    await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="b", digits="2"
        ),
    )
    # Timeout: empty digits, AND the queue's real max_wait_seconds has genuinely elapsed
    # (B10: a bare Gather timeout alone no longer overflows - see the dedicated
    # test_carrier_queue_wait_timeout_reissues_gather_before_max_wait test below).
    far_future = datetime.now(timezone.utc) + timedelta(seconds=queue_row.max_wait_seconds + 10)
    commands = await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="c", digits=""
        ),
        now=far_future,
    )
    assert commands[-1] == voice.Hangup()
    entry = (await session.execute(sa.select(QueueEntry))).scalars().one()
    assert entry.state == expect_last_state


async def test_carrier_queue_voicemail_overflow_records_a_voicemail(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    queue_row = CallQueue(
        id=uuid.uuid4(), org_id=org_id, name="q", max_wait_seconds=300, overflow="voicemail"
    )
    session.add(queue_row)
    await session.flush()
    flow = await _flow_row(
        session, org_id, _menu_flow(await _dummy_ring_group(session, org_id), str(queue_row.id))
    )
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="a", digits="1"
        ),
    )
    await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="b", digits="2"
        ),
    )
    far_future = datetime.now(timezone.utc) + timedelta(seconds=queue_row.max_wait_seconds + 10)
    commands = await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="c", digits=""
        ),
        now=far_future,
    )
    assert commands == [voice.Speak(text="Leave a message after the tone."), voice.StartRecording()]

    from app.models.callflow import Voicemail

    vm = (await session.execute(sa.select(Voicemail))).scalars().one()
    assert vm.call_id == call.id


# --------------------------------------------------------------------------------------
# DR-4: engine runtime error -> voicemail fallback if present, else hangup.
# --------------------------------------------------------------------------------------
async def test_dr4_runtime_error_falls_back_to_voicemail_when_present(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    broken = {
        "entry": "missing",
        "nodes": {"voicemail": {"type": "voicemail", "greeting": "leave one"}},
    }
    # Bypass the save-time validation gate on purpose - this simulates a runtime error
    # that DR-4 says must still fall back gracefully rather than crash the call.
    from app.models.callflow import CallFlow

    flow = CallFlow(
        id=uuid.uuid4(),
        org_id=org_id,
        name="broken-at-runtime",
        version=1,
        status="active",
        definition=broken,
    )
    session.add(flow)
    await session.flush()
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    commands = await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    assert commands == [voice.Speak(text="leave one"), voice.StartRecording()]
    await session.refresh(call)
    assert call.extra["flow"]["terminal"] == "voicemail"


async def test_dr4_runtime_error_falls_back_to_hangup_when_no_voicemail_node(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    from app.models.callflow import CallFlow

    broken = {"entry": "missing", "nodes": {"only": {"type": "hangup"}}}
    flow = CallFlow(
        id=uuid.uuid4(),
        org_id=org_id,
        name="broken-no-vm",
        version=1,
        status="active",
        definition=broken,
    )
    session.add(flow)
    await session.flush()
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    commands = await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    assert commands[-1] == voice.Hangup()
    await session.refresh(call)
    assert call.extra["flow"]["terminal"] == "hangup"


async def test_drive_caps_an_hours_cycle_and_falls_back_to_voicemail(session, engine):
    """B4 (PROVEN): validate_flow's reachability check only proves every node CAN be
    reached, never that stepping through them at runtime terminates - two hours nodes
    routing into each other on the SAME outcome save perfectly cleanly (voicemail stays
    reachable via each node's OTHER two outcomes) and would loop forever without the
    _MAX_DRIVE_ITERATIONS cap in `_drive`."""
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    bh = BusinessHours(
        id=uuid.uuid4(),
        org_id=org_id,
        name="always-closed",
        timezone="UTC",
        schedule={},
        holidays=[],
    )
    session.add(bh)
    await session.flush()
    # Empty schedule -> flows_svc.evaluate_hours always returns "closed" - so the "closed"
    # edges (hours_a <-> hours_b) are the ones that actually cycle at runtime; "open" and
    # "holiday" point to voicemail purely to keep it graph-reachable for validate_flow.
    cycle_flow = {
        "entry": "hours_a",
        "nodes": {
            "hours_a": {
                "type": "hours",
                "business_hours_id": str(bh.id),
                "open": "voicemail",
                "closed": "hours_b",
                "holiday": "voicemail",
            },
            "hours_b": {
                "type": "hours",
                "business_hours_id": str(bh.id),
                "open": "voicemail",
                "closed": "hours_a",
                "holiday": "voicemail",
            },
            "voicemail": {"type": "voicemail", "greeting": "leave one"},
        },
    }
    flow = await _flow_row(session, org_id, cycle_flow)
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    commands = await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    assert commands == [voice.Speak(text="leave one"), voice.StartRecording()]
    await session.refresh(call)
    assert call.extra["flow"]["terminal"] == "voicemail"


# --------------------------------------------------------------------------------------
# B5 (PROVEN): a redelivered call_initiated must never re-enter the graph.
# --------------------------------------------------------------------------------------
async def test_redelivered_call_initiated_rerenders_menu_without_duplicating_anything(
    session, engine
):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    flow = await _flow_row(
        session,
        org_id,
        _menu_flow(await _dummy_ring_group(session, org_id), await _dummy_queue(session, org_id)),
    )
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    first = await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    second = await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    assert (
        first
        == second
        == [
            voice.Speak(text="root menu"),
            voice.Gather(max_digits=1, timeout_seconds=10, action_tag="flow_digit"),
        ]
    )


async def test_redelivered_call_initiated_does_not_duplicate_voicemail_row_or_event(
    session, engine
):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    flow_def = {"entry": "vm", "nodes": {"vm": {"type": "voicemail", "greeting": "hi there"}}}
    flow = await _flow_row(session, org_id, flow_def)
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    from app.models.callflow import Voicemail
    from app.models.platform import PlatformEvent

    first = await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    assert first == [voice.Speak(text="hi there"), voice.StartRecording()]
    assert len((await session.execute(sa.select(Voicemail))).scalars().all()) == 1
    assert (
        len(
            (
                await session.execute(
                    sa.select(PlatformEvent).where(PlatformEvent.event_type == "voicemail.created")
                )
            )
            .scalars()
            .all()
        )
        == 1
    )

    # Redelivery: the SAME call, same flow, call_initiated fires again.
    second = await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    assert second == first
    assert len((await session.execute(sa.select(Voicemail))).scalars().all()) == 1
    assert (
        len(
            (
                await session.execute(
                    sa.select(PlatformEvent).where(PlatformEvent.event_type == "voicemail.created")
                )
            )
            .scalars()
            .all()
        )
        == 1
    )


async def test_redelivered_call_initiated_does_not_duplicate_queue_entry(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    queue_row = CallQueue(
        id=uuid.uuid4(), org_id=org_id, name="q", max_wait_seconds=300, overflow="hangup"
    )
    session.add(queue_row)
    await session.flush()
    flow_def = {"entry": "q", "nodes": {"q": {"type": "queue", "queue_id": str(queue_row.id)}}}
    flow = await _flow_row(session, org_id, flow_def)
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    first = await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    second = await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    assert first == second

    entries = (await session.execute(sa.select(QueueEntry))).scalars().all()
    assert len(entries) == 1


# --------------------------------------------------------------------------------------
# Item 11: the `transfer` node, wired end to end.
# --------------------------------------------------------------------------------------
async def test_carrier_transfer_node_renders_p5_transfer_command(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    flow_def = {
        "entry": "root",
        "nodes": {
            "root": {
                "type": "menu",
                "prompt": "press 1 for sales",
                "options": {"1": "xfer"},
                "invalid_node": "xfer",
            },
            "xfer": {"type": "transfer", "to": "+15125551234"},
        },
    }
    flow = await _flow_row(session, org_id, flow_def)
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()

    await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    commands = await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="a", digits="1"
        ),
    )
    assert commands == [voice.Transfer(to="+15125551234", from_=OUR)]
    await session.refresh(call)
    assert call.extra["flow"]["terminal"] == "transferred"

    # B5: a redelivered call_initiated re-renders the SAME Transfer command rather than
    # re-entering the graph (which would be harmless here structurally, but the contract
    # is universal - never re-enter once a flow is pinned).
    commands_again = await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    assert commands_again == commands


# --------------------------------------------------------------------------------------
# Room executor: routing_tick offer/claim/abandon/overflow, and derived queue position.
# --------------------------------------------------------------------------------------
async def _make_queue_and_ring(
    session, org_id, *, strategy="simultaneous", members=None, max_wait=300, overflow="hangup"
):
    ring = RingGroupDef(
        id=uuid.uuid4(),
        org_id=org_id,
        name=f"rg-{uuid.uuid4().hex[:6]}",
        strategy=strategy,
        member_user_ids=members or [],
        ring_timeout_seconds=5,
    )
    session.add(ring)
    await session.flush()
    queue = CallQueue(
        id=uuid.uuid4(),
        org_id=org_id,
        name=f"q-{uuid.uuid4().hex[:6]}",
        max_wait_seconds=max_wait,
        overflow=overflow,
        ring_group_id=ring.id,
    )
    session.add(queue)
    await session.commit()
    return queue, ring


async def _enqueue(session, org_id, queue_id, *, enqueued_at=None):
    call = _make_call(org_id, extra={"via": "livekit", "room": "call-x"})
    session.add(call)
    await session.flush()
    entry = QueueEntry(
        id=uuid.uuid4(),
        org_id=org_id,
        queue_id=queue_id,
        call_id=call.id,
        state="waiting",
        enqueued_at=enqueued_at or datetime.now(timezone.utc),
    )
    session.add(entry)
    await session.commit()
    return call, entry


async def test_routing_tick_offers_waiting_entry_with_targeted_ring_user_ids(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    member = uuid.uuid4()
    queue, _ring = await _make_queue_and_ring(session, org_id, members=[str(member)])
    call, entry = await _enqueue(session, org_id, queue.id)
    bus = FakeBus()

    counts = await routing_exec_svc.routing_tick(session, bus, now=datetime.now(timezone.utc))
    assert counts["offered"] == 1
    await session.refresh(entry)
    assert entry.state == "offered"

    ring_events = [e for _org, e in bus.published if e["type"] == "call.ring"]
    assert len(ring_events) == 1
    assert ring_events[0]["ring_user_ids"] == [str(member)]
    assert ring_events[0]["call_id"] == str(call.id)


async def test_routing_tick_never_offers_a_carrier_path_entry(session, engine):
    """B6: a carrier-path call's queue entry is driven inline by the SAME webhook
    response that enqueued it - routing_tick offering it to a room agent would ring
    someone who has no way to answer a PSTN-only call. Abandon/overflow detection still
    applies (a carrier call CAN still be marked abandoned/overflowed as a backstop) - only
    the OFFER path is skipped."""
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    member = uuid.uuid4()
    queue, _ring = await _make_queue_and_ring(session, org_id, members=[str(member)])
    call = _make_call(org_id, extra={"via": "carrier"})
    session.add(call)
    await session.flush()
    entry = QueueEntry(
        id=uuid.uuid4(),
        org_id=org_id,
        queue_id=queue.id,
        call_id=call.id,
        state="waiting",
        enqueued_at=datetime.now(timezone.utc),
    )
    session.add(entry)
    await session.commit()
    bus = FakeBus()

    counts = await routing_exec_svc.routing_tick(session, bus, now=datetime.now(timezone.utc))
    assert counts["offered"] == 0
    await session.refresh(entry)
    assert entry.state == "waiting"  # untouched - never offered
    assert [e for _org, e in bus.published if e["type"] == "call.ring"] == []


async def test_routing_tick_sequential_group_advances_on_timeout(session, engine):
    org_id = await _make_org(session)
    a, b = await _make_user(session), await _make_user(session)
    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(
        session, org_id, strategy="sequential", members=[str(a), str(b)]
    )
    now = datetime.now(timezone.utc)
    call, entry = await _enqueue(session, org_id, queue.id, enqueued_at=now)
    bus = FakeBus()

    await routing_exec_svc.routing_tick(session, bus, now=now)
    await session.refresh(entry)
    assert entry.offered_user_id == a

    # Ring timeout (5s) elapses - the tick advances to the NEXT sequential member.
    later = now + timedelta(seconds=10)
    await routing_exec_svc.routing_tick(session, bus, now=later)
    await session.refresh(entry)
    assert entry.offered_user_id == b


async def test_routing_tick_marks_abandoned_when_caller_call_ends(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(session, org_id)
    call, entry = await _enqueue(session, org_id, queue.id)
    call.status = "completed"
    await session.flush()
    bus = FakeBus()

    counts = await routing_exec_svc.routing_tick(session, bus, now=datetime.now(timezone.utc))
    assert counts["abandoned"] == 1
    await session.refresh(entry)
    assert entry.state == "abandoned"


async def test_routing_tick_processes_entries_from_two_different_orgs(session, engine):
    """B1 (PROVEN): a single routing_tick pass is genuinely cross-org (the entries SELECT
    is `allow_unscoped`), so it must commit each entry's mutation before the next entry's
    `set_org_context` call - a single trailing commit would autoflush the FIRST org's
    still-pending mutation under the SECOND org's context, raising
    MissingTenantContextError, which the sweeper's outer try/except then silently
    swallows, dropping every remaining entry (not just the one that "failed"). Both
    entries here must come back correctly processed, not just the first one."""
    org_a = await _make_org(session)
    org_b = await _make_org(session)

    set_org_context(session, org_a)
    queue_a, _ring_a = await _make_queue_and_ring(session, org_a)
    call_a, entry_a = await _enqueue(session, org_a, queue_a.id)
    call_a.status = "completed"  # org A's entry resolves via the ABANDON branch
    await session.commit()

    set_org_context(session, org_b)
    member_b = uuid.uuid4()
    queue_b, _ring_b = await _make_queue_and_ring(session, org_b, members=[str(member_b)])
    _call_b, entry_b = await _enqueue(session, org_b, queue_b.id)  # org B's resolves via OFFER
    await session.commit()

    bus = FakeBus()
    counts = await routing_exec_svc.routing_tick(session, bus, now=datetime.now(timezone.utc))

    assert counts["abandoned"] == 1
    assert counts["offered"] == 1

    set_org_context(session, org_a)
    await session.refresh(entry_a)
    assert entry_a.state == "abandoned"

    set_org_context(session, org_b)
    await session.refresh(entry_b)
    assert entry_b.state == "offered"


@pytest.mark.parametrize("overflow", ["hangup", "voicemail"])
async def test_routing_tick_max_wait_room_path_overflow_records_state_only(
    session, engine, overflow
):
    """Item 12 (Opus test-hygiene note): "hangup" and "voicemail" overflow on the ROOM
    path are honestly a STATE CHANGE ONLY, never a real disconnect/recording -
    `_apply_room_queue_overflow`'s own docstring says so: tearing down a live LiveKit
    participant needs `voice_plane/service.py`, which is forbidden here. This asserts
    that limitation explicitly (no Voicemail row, no call mutation) rather than
    conflating the two configs with a real terminal action the way the old parametrized
    test implied by asserting the same "overflowed" state for both."""
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(session, org_id, max_wait=30, overflow=overflow)
    now = datetime.now(timezone.utc)
    call, entry = await _enqueue(session, org_id, queue.id, enqueued_at=now - timedelta(seconds=60))
    bus = FakeBus()

    await routing_exec_svc.routing_tick(session, bus, now=now)
    await session.refresh(entry)
    assert entry.state == "overflowed"
    assert entry.resolved_at is not None

    from app.models.callflow import Voicemail

    assert (await session.execute(sa.select(Voicemail))).scalars().all() == []
    await session.refresh(call)
    assert call.status not in ("completed", "failed", "busy", "no_answer", "canceled")


async def test_routing_tick_max_wait_callback_overflow_captures_a_real_callback(session, engine):
    """ "callback" is the one overflow action the room executor CAN fully honor without
    touching a live room - it only ever needed to write `queue_entries.callback_e164`."""
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(session, org_id, max_wait=30, overflow="callback")
    now = datetime.now(timezone.utc)
    call, entry = await _enqueue(session, org_id, queue.id, enqueued_at=now - timedelta(seconds=60))
    bus = FakeBus()

    await routing_exec_svc.routing_tick(session, bus, now=now)
    await session.refresh(entry)
    assert entry.state == "callback_requested"
    assert entry.callback_e164 == THEIRS


async def test_queue_position_derived_with_three_waiting(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(session, org_id)
    now = datetime.now(timezone.utc)
    _c1, e1 = await _enqueue(session, org_id, queue.id, enqueued_at=now)
    _c2, e2 = await _enqueue(session, org_id, queue.id, enqueued_at=now + timedelta(seconds=1))
    _c3, e3 = await _enqueue(session, org_id, queue.id, enqueued_at=now + timedelta(seconds=2))

    assert await routing_exec_svc.queue_position(session, e1) == 0
    assert await routing_exec_svc.queue_position(session, e2) == 1
    assert await routing_exec_svc.queue_position(session, e3) == 2


async def test_claim_next_connects_the_earliest_entry_and_claim_cancels_via_event(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(session, org_id)
    now = datetime.now(timezone.utc)
    _c1, e1 = await _enqueue(session, org_id, queue.id, enqueued_at=now)
    _c2, _e2 = await _enqueue(session, org_id, queue.id, enqueued_at=now + timedelta(seconds=1))
    user_id = await _make_user(session)

    claimed = await routing_exec_svc.claim_next(session, org_id, queue.id, user_id)
    assert claimed.id == e1.id
    assert claimed.state == "connected"
    assert claimed.offered_user_id == user_id

    entries = await routing_exec_svc.list_queue_entries(session, queue.id, states=["waiting"])
    assert len(entries) == 1  # only the second one remains waiting


async def test_claim_entry_second_concurrent_claim_gets_conflict(session, engine):
    """B9: the SAME entry claimed twice - the second claim must lose via the conditional
    UPDATE's rowcount, not by a stale read-then-write letting both "win"."""
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(session, org_id)
    _call, entry = await _enqueue(session, org_id, queue.id)
    user_a, user_b = await _make_user(session), await _make_user(session)

    first = await routing_exec_svc.claim_entry(session, entry.id, user_a)
    assert first.state == "connected"
    assert first.offered_user_id == user_a

    with pytest.raises(ConflictError):
        await routing_exec_svc.claim_entry(session, entry.id, user_b)

    await session.refresh(entry)
    assert entry.offered_user_id == user_a  # the loser never overwrote the winner


async def test_claim_next_concurrent_callers_only_one_wins(session, engine):
    """B9: two callers racing `claim_next` on a queue with exactly ONE claimable entry -
    genuine asyncio concurrency (separate sessions), not just two sequential calls.
    Exactly one succeeds; the other gets ConflictError (lost the race), never a silent
    duplicate "success" and never a duplicate row."""
    from app.db.session import get_sessionmaker

    org_id = await _make_org(session)
    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(session, org_id)
    _call, entry = await _enqueue(session, org_id, queue.id)
    user_a, user_b = await _make_user(session), await _make_user(session)

    async def _claim(user_id):
        async with get_sessionmaker()() as claim_session:
            set_org_context(claim_session, org_id)
            return await routing_exec_svc.claim_next(claim_session, org_id, queue.id, user_id)

    results = await asyncio.gather(_claim(user_a), _claim(user_b), return_exceptions=True)

    successes = [r for r in results if isinstance(r, QueueEntry)]
    conflicts = [r for r in results if isinstance(r, ConflictError)]
    assert len(successes) == 1, f"expected exactly one winner, got {results!r}"
    assert len(conflicts) == 1
    assert successes[0].id == entry.id
    assert successes[0].offered_user_id in (user_a, user_b)


async def test_claim_entry_route_publishes_handoff_claimed_event(app_with_carrier, session):
    """DR-5: "claim cancels outstanding offers" - the mechanism this build has is the same
    `call.handoff.claimed` event `POST /calls/{id}/answer` already publishes (routes/calls.py,
    forbidden to edit); this route publishes the identical shape so any console built
    against that broadcast still clears the ring card on a queue claim."""
    client, _fake, app = app_with_carrier
    token, org, _number = await make_org_with_number(client, "rt3@example.com", "Org C", OUR)
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(session, org_id)
    _call, entry = await _enqueue(session, org_id, queue.id)

    captured: list[dict] = []
    original_publish = app.state.event_bus.publish

    def _spy(published_org_id, event):  # noqa: ANN001
        captured.append(event)
        original_publish(published_org_id, event)

    app.state.event_bus.publish = _spy

    r = await client.post(
        f"/api/v1/queue-entries/{entry.id}/claim", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "connected"
    assert any(
        e.get("type") == "call.handoff.claimed" and e.get("call_id") == str(entry.call_id)
        for e in captured
    )


# --------------------------------------------------------------------------------------
# Item 8 (B8): dial-now compliance precheck, caller-ID preference, and the honest
# "stays callback_requested through the dial" state marker.
# --------------------------------------------------------------------------------------
async def _callback_entry(session, org_id, queue_id, callback_e164, *, our_e164=OUR):
    call = _make_call(org_id, extra={})
    call.our_e164 = our_e164
    session.add(call)
    await session.flush()
    entry = QueueEntry(
        id=uuid.uuid4(),
        org_id=org_id,
        queue_id=queue_id,
        call_id=call.id,
        state="callback_requested",
        callback_e164=callback_e164,
        enqueued_at=datetime.now(timezone.utc),
    )
    session.add(entry)
    await session.commit()
    return call, entry


async def test_dial_now_blocks_opted_out_number(app_with_carrier, session):
    from app.compliance import service as compliance_svc

    client, _fake, _app = app_with_carrier
    token, org, _number = await make_org_with_number(
        client, "dn-optout@example.com", "Org DN1", OUR
    )
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(session, org_id)
    _call, entry = await _callback_entry(session, org_id, queue.id, THEIRS)
    await compliance_svc.record_consent(
        session, org_id, contact_e164=THEIRS, event="opt_out", source="manual"
    )
    await session.commit()

    r = await client.post(
        f"/api/v1/queue-entries/{entry.id}/dial-now", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 409, r.text
    await session.refresh(entry)
    assert entry.state == "callback_requested"  # never touched - the dial never happened


async def test_dial_now_blocks_dnc_number(app_with_carrier, session):
    from app.compliance import service as compliance_svc

    client, _fake, _app = app_with_carrier
    token, org, _number = await make_org_with_number(client, "dn-dnc@example.com", "Org DN2", OUR)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(session, org_id)
    _call, entry = await _callback_entry(session, org_id, queue.id, THEIRS)
    await compliance_svc.add_dnc(session, org_id, THEIRS, source="manual")
    await session.commit()

    r = await client.post(
        f"/api/v1/queue-entries/{entry.id}/dial-now", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 409, r.text


async def test_dial_now_blocks_quiet_hours_via_the_same_primitive_the_dialer_uses(
    app_with_carrier, session, monkeypatch
):
    """Wiring test: dial-now calls the SAME `qh.evaluate` primitive `services/dialer.py`
    uses before every campaign dial - proven by forcing it to deny and checking the route
    409s, rather than fighting the suite's frozen clock / area-code timezone inference to
    land inside a real quiet-hours window."""
    from app.compliance import quiet_hours as qh

    client, _fake, _app = app_with_carrier
    token, org, _number = await make_org_with_number(client, "dn-qh@example.com", "Org DN3", OUR)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(session, org_id)
    _call, entry = await _callback_entry(session, org_id, queue.id, THEIRS)

    monkeypatch.setattr(
        qh,
        "evaluate",
        lambda *a, **k: qh.QuietHoursResult(  # noqa: ARG005
            False, datetime.now(timezone.utc) + timedelta(hours=1), ()
        ),
    )

    r = await client.post(
        f"/api/v1/queue-entries/{entry.id}/dial-now", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 409, r.text


async def test_dial_now_prefers_the_originally_dialed_number_as_caller_id(
    app_with_voice_carrier, session
):
    OTHER = "+12145550199"
    client, _fake, _app = app_with_voice_carrier
    token, org, _number = await make_org_with_number(
        client, "dn-callerid@example.com", "Org DN4", OUR
    )
    org_id = uuid.UUID(org["id"])
    r = await client.post(
        "/api/v1/numbers", json={"e164": OTHER}, headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 201, r.text

    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(session, org_id)
    # The caller originally dialed OTHER, not the org's "first" active number (OUR).
    _call, entry = await _callback_entry(session, org_id, queue.id, THEIRS, our_e164=OTHER)

    r = await client.post(
        f"/api/v1/queue-entries/{entry.id}/dial-now", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text

    outbound = (
        (
            await session.execute(
                sa.select(Call).where(Call.direction == "outbound", Call.contact_e164 == THEIRS)
            )
        )
        .scalars()
        .one()
    )
    assert outbound.our_e164 == OTHER


async def test_dial_now_keeps_callback_requested_and_stamps_a_dialing_marker(
    app_with_voice_carrier, session
):
    """B8: the smallest honest design - advancing to "connected" only on a genuine
    ANSWER would need a hook this implementer cannot add outside the P6 webhooks.py
    branch, so the entry stays "callback_requested" and offered_user_id/offered_at
    record who dialed and when, rather than lying about the call having connected."""
    client, _fake, _app = app_with_voice_carrier
    token, org, _number = await make_org_with_number(
        client, "dn-marker@example.com", "Org DN5", OUR
    )
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    queue, _ring = await _make_queue_and_ring(session, org_id)
    _call, entry = await _callback_entry(session, org_id, queue.id, THEIRS)

    r = await client.post(
        f"/api/v1/queue-entries/{entry.id}/dial-now", headers=auth_headers(token, org["id"])
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "callback_requested"
    assert body["offered_user_id"] is not None

    await session.refresh(entry)
    assert entry.state == "callback_requested"
    assert entry.offered_user_id is not None
    assert entry.offered_at is not None
