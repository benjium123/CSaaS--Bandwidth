"""Regression tests for BUGFIX_LEDGER_2026-09.md Area 3 (voice / media plane), plus the
1.3/1.10 webhook-ingress items folded into this batch and the 3.12 backend-half softphone
fan-out fix (also covered from the pure-function angle in test_p15_inbox_access.py)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest

from app.api.routes.webhooks import _voice_bxml_response
from app.db.base import set_org_context
from app.db.session import get_sessionmaker
from app.errors import ValidationFailedError
from app.main import create_app
from app.models.callflow import BusinessHours
from app.models.voice import Call, CallLeg
from app.providers.bandwidth.voice import BandwidthVoiceMixin, _bandwidth_event_id
from app.providers.telnyx.voice import TelnyxVoiceCommandError, TelnyxVoiceMixin
from app.providers.voice import Speak
from app.services.flows import create_business_hours, evaluate_hours
from app.voice_plane.livekit_api import LiveKitApi, LiveKitApiError
from app.voice_plane import service as voice_service
from tests.conftest import (
    WEBHOOK_PASS,
    WEBHOOK_USER,
    auth_headers,
    make_org_with_number,
    make_settings,
)
from tests.test_voice_webhooks import FakeVoiceCarrier, install_voice_carrier


@pytest.fixture
async def app_with_voice_carrier(engine):
    """App wired with a FakeVoiceCarrier named 'bandwidth' - local to this file, same
    convention every other voice test module follows (avoids cross-module fixture
    coupling)."""
    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    fake = FakeVoiceCarrier()
    install_voice_carrier(application, fake)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, application


async def test_3_1_livekit_client_uses_long_read_timeout():
    api = LiveKitApi(url="ws://127.0.0.1:7880", api_key="k", api_secret="s")
    client = await api._get_client()
    assert client.timeout.connect == 10
    assert client.timeout.read == 90
    await api.aclose()


async def test_3_2_telnyx_voice_commands_are_executed_when_render_returns_none():
    class TelnyxLike:
        def __init__(self):
            self.executed = None

        def render_commands(self, commands):
            return None

        async def execute_commands(self, provider_call_id, commands):
            self.executed = (provider_call_id, commands)

    carrier = TelnyxLike()
    commands = [Speak(text="hello")]
    resp = await _voice_bxml_response(carrier, commands, "call-123")
    assert resp.status_code == 200
    assert carrier.executed == ("call-123", commands)


# ----------------------------------------------------------------------------------
# B5: commands with no provider_call_id must never silently 200 with no audio played
# ----------------------------------------------------------------------------------
async def test_b5_commands_without_call_id_are_dead_lettered_and_502(session):
    import sqlalchemy as sa

    from app.models.messaging import WebhookDeadLetter

    class TelnyxLike:
        def render_commands(self, commands):
            return None

        async def execute_commands(self, provider_call_id, commands):  # pragma: no cover
            raise AssertionError("must never execute commands with no provider_call_id")

    carrier = TelnyxLike()
    commands = [Speak(text="hello")]
    resp = await _voice_bxml_response(
        carrier,
        commands,
        None,
        session=session,
        carrier_name="telnyx",
        body_text="raw-body",
    )
    assert resp.status_code == 502

    row = (
        await session.execute(
            sa.select(WebhookDeadLetter).where(WebhookDeadLetter.carrier == "telnyx")
        )
    ).scalar_one()
    assert row.reason == "commands_without_call_id"
    assert row.payload == "raw-body"


async def test_3_3_and_3_4_answer_call_advances_leg_and_only_first_claim_wins(engine):
    settings = make_settings(
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="lk-key",
        livekit_api_secret="lk-secret-value-padded-to-32-bytes-plus",
        livekit_sip_outbound_trunk_id="trunk-out-1",
    )
    application = create_app(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    lk_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application.state.livekit = LiveKitApi(
        url="ws://127.0.0.1:7880",
        api_key="lk-key",
        api_secret="lk-secret-value-padded-to-32-bytes-plus",
        client=lk_client,
    )

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token, org, _ = await make_org_with_number(
            client, "answer-owner@example.com", "Answer Org", "+12145550100"
        )
        org_id = uuid.UUID(org["id"])

        async with get_sessionmaker()() as session:
            set_org_context(session, org_id)
            call_id = uuid.uuid4()
            leg_id = uuid.uuid4()
            call = Call(
                id=call_id,
                org_id=org_id,
                direction="inbound",
                contact_e164="+19725550101",
                our_e164="+12145550100",
                carrier="telnyx",
                status="ringing",
                extra={"via": "livekit", "room": f"call-{call_id}"},
            )
            leg = CallLeg(
                id=leg_id,
                org_id=org_id,
                call_id=call_id,
                provider_call_id=f"lk-{call_id}",
                to_e164="+12145550100",
                from_e164="+19725550101",
                status="ringing",
                reason="original",
            )
            session.add(call)
            session.add(leg)
            await session.commit()

        headers = auth_headers(token, org_id)
        first = await client.post(f"/api/v1/calls/{call_id}/answer", headers=headers)
        assert first.status_code == 200, first.text

        second = await client.post(f"/api/v1/calls/{call_id}/answer", headers=headers)
        assert second.status_code == 409, second.text

        async with get_sessionmaker()() as session:
            set_org_context(session, org_id)
            leg_after = await session.get(CallLeg, leg_id)
            assert leg_after.status == "answered"
            assert leg_after.answered_at is not None

    await lk_client.aclose()


def test_3_6_bandwidth_voice_guard_fails_closed_when_only_one_credential_is_blank():
    """B3: with headers={} this assertion passes whether the guard is `not user and not
    password` (old, buggy - only fails closed when BOTH are blank) or `not user or not
    password` (new, fixed) - the header check below never even runs since the auth
    header doesn't match either way. Use a header that DOES pass the username/blank-
    password check: "Basic dXNlcjo=" decodes to "user:" (username "user", password "").
    Under the old AND-guard, one blank credential does not fail closed, so this would
    reach the header comparison and MATCH (username "user" == "user", password "" ==
    "") -> True, a real auth bypass whenever only a password is left unconfigured. The
    new OR-guard must reject it before the header is ever inspected."""
    class B(BandwidthVoiceMixin):
        voice_webhook_username = "user"
        voice_webhook_password = ""

    b = B()
    assert b.verify_voice_webhook({"Authorization": "Basic dXNlcjo="}, b"") is False


def test_3_16_transport_error_is_failed_not_no_answer():
    exc = LiveKitApiError(0, "httpx.ConnectTimeout")
    assert voice_service._dial_error_leg_status(exc) == ("failed", "")
    exc2 = LiveKitApiError(408, "timeout")
    assert voice_service._dial_error_leg_status(exc2) == ("hungup", "no_answer")


async def test_3_15_telnyx_execute_commands_raises_on_bad_status():
    class T(TelnyxVoiceMixin):
        def __init__(self, client):
            self.base_url = "https://api.telnyx.com/v2"
            self.api_key = "k"
            self._client = client

        async def _get_client(self):
            return self._client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        t = T(client)
        with pytest.raises(TelnyxVoiceCommandError):
            await t.execute_commands("call-id", [Speak(text="hi")])


def test_3_20_bandwidth_event_id_discriminates_same_timestamp_dtmf():
    id1 = _bandwidth_event_id("dtmf_received", "c1", "2026-01-01T00:00:00Z", "digit:1")
    id2 = _bandwidth_event_id("dtmf_received", "c1", "2026-01-01T00:00:00Z", "digit:2")
    assert id1 != id2


def test_3_23_evaluate_hours_wraps_past_midnight():
    bh = BusinessHours(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        name="test",
        timezone="UTC",
        schedule={"mon": [["22:00", "02:00"]]},
        holidays=[],
    )
    monday_evening = datetime(2026, 1, 5, 23, 0, tzinfo=timezone.utc)
    tuesday_morning = datetime(2026, 1, 6, 1, 0, tzinfo=timezone.utc)
    monday_morning = datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)

    assert evaluate_hours(bh, monday_evening) == "open"
    assert evaluate_hours(bh, tuesday_morning) == "open"
    assert evaluate_hours(bh, monday_morning) == "closed"


async def test_3_23_business_hours_reject_unknown_weekday_key(session):
    with pytest.raises(ValidationFailedError):
        await create_business_hours(
            session,
            org_id=uuid.uuid4(),
            name="bad",
            timezone_name="UTC",
            schedule={"monday": [["09:00", "17:00"]]},
            holidays=[],
        )


# --------------------------------------------------------------------------------------
# 3.13: dial-now claim is atomic - two concurrent clicks cannot both dial
# --------------------------------------------------------------------------------------
async def test_3_13_dial_now_claim_is_atomic(app_with_voice_carrier, session):
    import sqlalchemy as sa

    from app.errors import ConflictError
    from app.models.callflow import CallQueue, QueueEntry, RingGroupDef

    client, _fake, _app = app_with_voice_carrier
    OUR = "+12145550170"
    THEIRS = "+19725550171"
    token, org, _ = await make_org_with_number(client, "dn-atomic@example.com", "Org DN-A", OUR)
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)

    ring_group = RingGroupDef(
        id=uuid.uuid4(), org_id=org_id, name="RG", strategy="all", member_user_ids=[],
        ring_timeout_seconds=20,
    )
    session.add(ring_group)
    await session.flush()
    queue = CallQueue(
        id=uuid.uuid4(), org_id=org_id, name="Q", ring_group_id=ring_group.id,
        max_wait_seconds=30, overflow="callback",
    )
    session.add(queue)
    await session.flush()

    call = Call(
        id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164=THEIRS,
        our_e164=OUR, carrier="telnyx", status="completed",
    )
    session.add(call)
    await session.flush()
    entry = QueueEntry(
        id=uuid.uuid4(), org_id=org_id, queue_id=queue.id, call_id=call.id,
        state="callback_requested", callback_e164=THEIRS,
        enqueued_at=datetime.now(timezone.utc),
    )
    session.add(entry)
    await session.commit()

    h = auth_headers(token, org_id)
    r1 = await client.post(f"/api/v1/queue-entries/{entry.id}/dial-now", headers=h)
    assert r1.status_code == 200, r1.text

    # A second "dial now" on the SAME entry must not dial again - the claim must
    # already have consumed dial_now_claimed_at, not merely offered_at (which the
    # agent-offer flow can leave stale for reasons unrelated to this claim).
    r2 = await client.post(f"/api/v1/queue-entries/{entry.id}/dial-now", headers=h)
    assert r2.status_code == 409, r2.text

    outbound_calls = (
        await session.execute(
            sa.select(Call).where(Call.direction == "outbound", Call.contact_e164 == THEIRS)
        )
    ).scalars().all()
    assert len(outbound_calls) == 1

    await session.refresh(entry)
    # B8's existing "stays callback_requested through the dial" design is preserved.
    assert entry.state == "callback_requested"


async def test_3_13_dial_now_claim_ignores_stale_offered_at_from_a_prior_offer_cycle(
    app_with_voice_carrier, session
):
    """An entry that already went through an agent-offer cycle (offered_at set) before
    overflowing to callback_requested must still be dialable ONCE - a naive
    `offered_at IS NULL` claim guard would wrongly 409 this legitimate first click."""
    import uuid as _uuid

    from app.models.callflow import CallQueue, QueueEntry, RingGroupDef

    client, _fake, _app = app_with_voice_carrier
    OUR = "+12145550172"
    THEIRS = "+19725550173"
    token, org, _ = await make_org_with_number(client, "dn-stale@example.com", "Org DN-S", OUR)
    org_id = _uuid.UUID(org["id"])
    set_org_context(session, org_id)

    ring_group = RingGroupDef(
        id=_uuid.uuid4(), org_id=org_id, name="RG", strategy="all", member_user_ids=[],
        ring_timeout_seconds=20,
    )
    session.add(ring_group)
    await session.flush()
    queue = CallQueue(
        id=_uuid.uuid4(), org_id=org_id, name="Q", ring_group_id=ring_group.id,
        max_wait_seconds=30, overflow="callback",
    )
    session.add(queue)
    await session.flush()

    call = Call(
        id=_uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164=THEIRS,
        our_e164=OUR, carrier="telnyx", status="completed",
    )
    session.add(call)
    await session.flush()
    entry = QueueEntry(
        id=_uuid.uuid4(), org_id=org_id, queue_id=queue.id, call_id=call.id,
        state="callback_requested", callback_e164=THEIRS,
        enqueued_at=datetime.now(timezone.utc),
        # Simulates a prior agent-offer cycle that left offered_at set before this
        # entry overflowed into callback_requested.
        offered_at=datetime.now(timezone.utc),
    )
    session.add(entry)
    await session.commit()

    h = auth_headers(token, org_id)
    r = await client.post(f"/api/v1/queue-entries/{entry.id}/dial-now", headers=h)
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------------------
# 3.18: an fe.step failure inside _drive falls back instead of propagating (dead air)
# --------------------------------------------------------------------------------------
async def test_3_18_drive_falls_back_when_fe_step_raises_mid_loop(client, session, monkeypatch):
    from app.services import flow_engine as fe
    from app.services import routing_exec as routing_exec_svc
    from app.models.callflow import CallFlow

    token, org, _ = await make_org_with_number(
        client, "drive-fallback@example.com", "Org Drive", "+12145550199"
    )
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(), org_id=org_id, direction="inbound", contact_e164="+19725550199",
        our_e164="+12145550199", carrier="telnyx", status="ringing", extra={},
    )
    flow = CallFlow(
        id=uuid.uuid4(), org_id=org_id, name="F",
        definition={
            "start": "hours",
            "nodes": {
                "hours": {"type": "evaluate_hours", "business_hours_id": str(uuid.uuid4())},
                "voicemail": {"type": "voicemail", "greeting": "Leave a message."},
            },
        },
    )
    session.add_all([call, flow])
    await session.commit()

    result = fe.StepResult(
        state={"node": "hours"},
        actions=(fe.EvaluateHours(business_hours_id=str(uuid.uuid4())),),
        awaiting=None,
        terminal=None,
    )

    def _boom(*args, **kwargs):
        raise fe.FlowError("boom")

    monkeypatch.setattr(fe, "step", _boom)

    commands = await routing_exec_svc._drive(session, None, call, flow, result)
    # _fallback() always returns Speak+StartRecording (voicemail) or Speak+Hangup (no
    # voicemail node) - never an empty list (dead air).
    assert commands, "fe.step failure must fall back, not propagate to dead air"
