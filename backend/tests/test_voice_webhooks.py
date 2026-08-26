"""Phase 5: voice webhook ingestion.

Mirrors test_webhook_ingest.py's shape: the load-bearing assertions are about the state
machine surviving carrier retries (D6) - duplicates applying once, a stale event never
walking a fact backwards, and an unmatched event being stored and 200'd, never 404'd.

Two carrier doubles are used, deliberately different:
  * The REAL BandwidthMessagingCarrier (with its voice mixin) drives the webhook-ingestion
    tests below, POSTing raw JSON shaped exactly like the adapter's parse function expects
    - so parse_voice_webhook and verify_voice_webhook are genuinely exercised, not assumed.
  * `FakeVoiceCarrier`, defined here, is for tests that need to CONTROL and INSPECT
    create_call/execute_commands deterministically (imported by test_voice_api.py and
    test_voice_recordings.py too).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import ALLOW_UNSCOPED_KEY
from app.main import create_app
from app.models.voice import CallLeg
from app.models.voice import VoiceEvent as VoiceEventRow
from app.providers.registry import CarrierRegistry
from app.providers.voice import CreateCallResult, Hangup, Speak, VoiceEvent
from tests.conftest import WEBHOOK_PASS, WEBHOOK_USER, make_settings, webhook_auth_headers

OUR = "+12145550100"
THEIRS = "+19725550199"


async def _unscoped(session, model):
    stmt = sa.select(model).execution_options(**{ALLOW_UNSCOPED_KEY: True})
    return list((await session.execute(stmt)).scalars().all())


# ==================================================================================
# FakeVoiceCarrier - shared across test_voice_*.py
# ==================================================================================
@dataclass
class FakeVoiceCarrier:
    """Records every create_call/execute_commands invocation and returns scripted results.

    Deliberately implements ONLY the VoiceCarrier surface - no messaging methods - so a
    test that accidentally exercises the messaging path fails loudly with an AttributeError
    rather than silently succeeding against the wrong double.
    """

    name: str = "bandwidth"
    create_calls: list[dict] = field(default_factory=list)
    execute_calls: list[tuple] = field(default_factory=list)
    scripted_results: list[CreateCallResult] = field(default_factory=list)
    verify_result: bool = True
    events_to_return: list[VoiceEvent] = field(default_factory=list)
    recording_auth_result: tuple | None = None
    bxml_shaped: bool = False  # True mimics Bandwidth: render_commands returns a string.
    execute_commands_error: Exception | None = None

    async def create_call(self, *, to, from_, machine_detection="off", tag=""):
        self.create_calls.append(
            {"to": to, "from_": from_, "machine_detection": machine_detection, "tag": tag}
        )
        if self.scripted_results:
            return self.scripted_results.pop(0)
        return CreateCallResult("accepted", f"leg-{len(self.create_calls)}")

    def verify_voice_webhook(self, headers, raw_body) -> bool:
        return self.verify_result

    def parse_voice_webhook(self, raw_body) -> list[VoiceEvent]:
        return self.events_to_return

    def render_commands(self, commands) -> str | None:
        if not self.bxml_shaped:
            return None
        return f"<Response>{len(commands)}</Response>"

    async def execute_commands(self, provider_call_id, commands) -> None:
        self.execute_calls.append((provider_call_id, commands))
        if self.execute_commands_error is not None:
            raise self.execute_commands_error

    def recording_auth(self, url):
        return self.recording_auth_result


def install_voice_carrier(application, carrier) -> None:
    application.state.carriers = CarrierRegistry({carrier.name: carrier}, primary=carrier.name)
    application.state.carrier = carrier


@pytest.fixture
async def app_with_voice_carrier(engine):
    """App wired with a FakeVoiceCarrier named 'bandwidth' (matches the default carrier
    numbers.add_number stamps onto a freshly seeded OrgNumber)."""
    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    fake = FakeVoiceCarrier()
    install_voice_carrier(application, fake)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake, application


@pytest.fixture
async def app_with_bandwidth_voice(engine):
    """App wired with the REAL Bandwidth adapter, so the voice webhook route genuinely
    exercises verify_voice_webhook / parse_voice_webhook against raw JSON bodies."""
    from app.providers.bandwidth.adapter import BandwidthMessagingCarrier

    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    carrier = BandwidthMessagingCarrier(
        account_id="acct-1",
        api_username="api-user",
        api_password="api-pass",
        application_id="msg-app-id",
        webhook_username=WEBHOOK_USER,
        webhook_password=WEBHOOK_PASS,
    )
    carrier.voice_application_id = "voice-app-id"
    carrier.voice_callback_url = "https://example.test/api/v1/webhooks/bandwidth/voice"
    carrier.voice_webhook_username = WEBHOOK_USER
    carrier.voice_webhook_password = WEBHOOK_PASS
    install_voice_carrier(application, carrier)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, carrier, application


def _bw_event(event_type: str, call_id: str, *, to: str = OUR, from_: str = THEIRS, **extra):
    body = {
        "eventType": event_type,
        "callId": call_id,
        "to": to,
        "from": from_,
        "startTime": "2026-06-15T18:00:00Z",
    }
    body.update(extra)
    return body


ANSWER_URL = "/api/v1/webhooks/bandwidth/voice/answer"
DISCONNECT_URL = "/api/v1/webhooks/bandwidth/voice/disconnect"
AMD_URL = "/api/v1/webhooks/bandwidth/voice/amd"


async def _post(client, url, body: dict, headers=None):
    return await client.post(
        url, content=json.dumps(body).encode(), headers=headers or webhook_auth_headers()
    )


# ---------------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------------
async def test_bad_voice_webhook_auth_is_401_and_writes_nothing(app_with_bandwidth_voice, session):
    client, _, _ = app_with_bandwidth_voice
    r = await _post(
        client,
        ANSWER_URL,
        _bw_event("initiate", "call-1"),
        headers=webhook_auth_headers("wrong", "wrong"),
    )
    assert r.status_code == 401
    assert await _unscoped(session, VoiceEventRow) == []


async def test_unconfigured_voice_webhook_credentials_refuse_everything(engine):
    """F11 (already fixed by the architect in providers/bandwidth/voice.py -
    verify_voice_webhook): an adapter with NO voice_webhook_username/password configured
    must FAIL CLOSED - the old fail-open bypass (accept when both are blank) is gone."""
    from app.providers.bandwidth.adapter import BandwidthMessagingCarrier

    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    carrier = BandwidthMessagingCarrier(
        account_id="acct-1",
        api_username="api-user",
        api_password="api-pass",
        application_id="msg-app-id",
        webhook_username=WEBHOOK_USER,
        webhook_password=WEBHOOK_PASS,
    )
    # Deliberately leave voice_webhook_username / voice_webhook_password unset - the
    # deployment "forgot" to configure them, same as a missing env var in production.
    carrier.voice_application_id = "voice-app-id"
    carrier.voice_callback_url = "https://example.test/api/v1/webhooks/bandwidth/voice"
    install_voice_carrier(application, carrier)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await _post(c, ANSWER_URL, _bw_event("initiate", "call-unconfigured"))
    assert r.status_code == 401


# ---------------------------------------------------------------------------------
# Full inbound lifecycle
# ---------------------------------------------------------------------------------
async def test_inbound_lifecycle_initiated_ringing_answered_hungup(
    app_with_bandwidth_voice, session
):
    from tests.conftest import auth_headers, make_org_with_number

    client, _, _ = app_with_bandwidth_voice
    token, org, _ = await make_org_with_number(client, "in1@example.com", "Org A", OUR)

    call_id = "bw-leg-1"
    r = await _post(client, ANSWER_URL, _bw_event("initiate", call_id))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "Response" in r.text  # BXML: not yet configured -> Speak + Hangup

    r = await _post(client, ANSWER_URL, _bw_event("answer", call_id))
    assert r.status_code == 200

    r = await _post(
        client,
        DISCONNECT_URL,
        _bw_event("disconnect", call_id, cause="normal-clearing", duration="PT30S"),
    )
    assert r.status_code == 200

    legs = await _unscoped(session, CallLeg)
    assert len(legs) == 1
    assert legs[0].status == "hungup"
    assert legs[0].hangup_cause == "normal-clearing"

    from app.models import Call

    calls = await _unscoped(session, Call)
    assert len(calls) == 1
    assert calls[0].direction == "inbound"
    assert calls[0].status == "completed"
    assert calls[0].answered_at is not None
    assert calls[0].ended_at is not None
    assert calls[0].duration_seconds is not None

    resp = await client.get(f"/api/v1/calls/{calls[0].id}", headers=auth_headers(token, org["id"]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "completed"


async def test_duplicate_disconnect_is_deduped_by_db_constraint(app_with_bandwidth_voice, session):
    """THE P5 DEDUPE GATE: a redelivered event applies once. Not an application-level
    check - the DB unique constraint on (carrier, provider_event_id) IS the mechanism."""
    from tests.conftest import make_org_with_number

    client, _, _ = app_with_bandwidth_voice
    await make_org_with_number(client, "dup1@example.com", "Org A", OUR)

    call_id = "bw-leg-dup"
    await _post(client, ANSWER_URL, _bw_event("initiate", call_id))
    await _post(client, ANSWER_URL, _bw_event("answer", call_id))

    body = _bw_event("disconnect", call_id, cause="normal-clearing", duration="PT10S")
    first = await _post(client, DISCONNECT_URL, body)
    assert first.status_code == 200
    second = await _post(client, DISCONNECT_URL, body)  # byte-identical replay
    assert second.status_code == 200

    events = [e for e in await _unscoped(session, VoiceEventRow) if e.event_type == "call_hungup"]
    assert len(events) == 1, "a replayed webhook must not double-ledger"


async def test_out_of_order_hungup_before_answered_ignores_the_answer(
    app_with_bandwidth_voice, session
):
    """A stale `answered` arriving after `hungup` must not resurrect the leg (D6)."""
    from tests.conftest import make_org_with_number

    client, _, _ = app_with_bandwidth_voice
    await make_org_with_number(client, "ooo1@example.com", "Org A", OUR)

    call_id = "bw-leg-ooo"
    await _post(client, ANSWER_URL, _bw_event("initiate", call_id))
    hangup = await _post(
        client, DISCONNECT_URL, _bw_event("disconnect", call_id, cause="normal-clearing")
    )
    assert hangup.status_code == 200

    late_answer = await _post(client, ANSWER_URL, _bw_event("answer", call_id))
    assert late_answer.status_code == 200

    legs = await _unscoped(session, CallLeg)
    assert legs[0].status == "hungup", "the late answer must not overwrite the terminal status"
    assert legs[0].answered_at is None, "a leg that never truly answered must not look answered"


async def test_org_resolved_unknown_leg_non_initiate_event_is_a_500_retry(
    app_with_bandwidth_voice, session
):
    """F9b: the org resolves (`to` is one of ours) but no leg matches the event's
    provider_call_id, no pending transfer leg adopts it, and this is NOT a call_initiated -
    mirrors messaging's Outcome.RETRY: 500 so the carrier redelivers once the leg this
    event actually belongs to exists. Nothing is ledgered for a retried event."""
    from tests.conftest import make_org_with_number

    client, _, _ = app_with_bandwidth_voice
    await make_org_with_number(client, "unk1@example.com", "Org A", OUR)

    r = await _post(client, ANSWER_URL, _bw_event("gather", "ghost-call-id", digits="5"))
    assert r.status_code == 500

    assert await _unscoped(session, VoiceEventRow) == [], (
        "a retried event must not be ledgered - the retry must see a clean slate"
    )


async def test_org_resolved_unknown_leg_duplicate_delivery_of_a_stored_event_is_still_200(
    app_with_bandwidth_voice, session
):
    """The F9b RETRY path is for events that were NEVER stored. An event that already made
    it into the ledger (leg matched the first time) must still 200 on redelivery - the
    ordinary D6 dedupe path is untouched by the F4/F9b changes."""
    from tests.conftest import make_org_with_number

    client, _, _ = app_with_bandwidth_voice
    await make_org_with_number(client, "unk2@example.com", "Org A", OUR)

    call_id = "bw-leg-dup-still-200"
    await _post(client, ANSWER_URL, _bw_event("initiate", call_id))

    body = _bw_event("answer", call_id)
    first = await _post(client, ANSWER_URL, body)
    assert first.status_code == 200
    second = await _post(client, ANSWER_URL, body)  # byte-identical replay
    assert second.status_code == 200


async def test_org_unresolvable_event_is_dead_lettered_and_200_not_404(
    app_with_bandwidth_voice, session
):
    """F12: when NEITHER `to` nor `from` resolves to any org at all, the event is
    dead-lettered and 200'd, never a 404 or a 500 - there is no org context in which to
    even ledger a VoiceEvent row, so dead-lettering IS "stored" for this case."""
    from app.models import WebhookDeadLetter

    client, _, _ = app_with_bandwidth_voice

    r = await _post(
        client,
        ANSWER_URL,
        _bw_event("gather", "ghost-call-id-2", to="+19995550000", from_="+19995550001", digits="5"),
    )
    assert r.status_code == 200

    assert await _unscoped(session, VoiceEventRow) == []
    dead_letters = await _unscoped(session, WebhookDeadLetter)
    assert len(dead_letters) == 1
    assert dead_letters[0].reason == "unknown_voice_call"


async def test_call_initiated_with_empty_provider_call_id_is_dead_lettered_never_a_call(
    app_with_bandwidth_voice, session
):
    """F13: an initiate with no id to key a leg on must never create a Call."""
    from app.models import Call, WebhookDeadLetter
    from tests.conftest import make_org_with_number

    client, _, _ = app_with_bandwidth_voice
    await make_org_with_number(client, "empty1@example.com", "Org A", OUR)

    r = await _post(client, ANSWER_URL, _bw_event("initiate", ""))
    assert r.status_code == 200

    assert await _unscoped(session, Call) == []
    dead_letters = [
        d
        for d in await _unscoped(session, WebhookDeadLetter)
        if d.reason == "empty_provider_call_id"
    ]
    assert len(dead_letters) == 1


async def test_amd_second_verdict_on_the_same_leg_is_ignored(app_with_bandwidth_voice, session):
    """F14: AMD is monotonic - the FIRST verdict wins even if a later (possibly
    contradictory) async callback names the same leg."""
    from tests.conftest import make_org_with_number

    client, _, _ = app_with_bandwidth_voice
    await make_org_with_number(client, "amd2@example.com", "Org A", OUR)

    call_id = "bw-leg-amd2"
    await _post(client, ANSWER_URL, _bw_event("initiate", call_id))
    await _post(
        client,
        AMD_URL,
        _bw_event(
            "machineDetectionComplete",
            call_id,
            machineDetectionResult={"value": "answering-machine-beep"},
        ),
    )
    legs = await _unscoped(session, CallLeg)
    assert legs[0].amd_result == "machine"

    # A second, contradictory verdict for the SAME leg must not overwrite the first. A
    # distinct startTime keeps this from deduping as the SAME event on provider_event_id -
    # this must be tested as a second, genuinely new AMD callback, not a replay.
    await _post(
        client,
        AMD_URL,
        _bw_event(
            "machineDetectionComplete",
            call_id,
            machineDetectionResult={"value": "human"},
            startTime="2026-06-15T18:05:00Z",
        ),
    )
    legs = await _unscoped(session, CallLeg)
    assert legs[0].amd_result == "machine", "the first AMD verdict must win"


async def test_amd_machine_detected_recorded_on_the_right_leg(app_with_bandwidth_voice, session):
    from tests.conftest import make_org_with_number

    client, _, _ = app_with_bandwidth_voice
    await make_org_with_number(client, "amd1@example.com", "Org A", OUR)

    call_id = "bw-leg-amd"
    await _post(client, ANSWER_URL, _bw_event("initiate", call_id))
    r = await _post(
        client,
        AMD_URL,
        _bw_event(
            "machineDetectionComplete",
            call_id,
            machineDetectionResult={"value": "answering-machine-beep"},
        ),
    )
    assert r.status_code == 200

    legs = await _unscoped(session, CallLeg)
    assert len(legs) == 1
    assert legs[0].amd_result == "machine"
    assert legs[0].status == "dialing", "AMD must not change the leg's call state"


# ---------------------------------------------------------------------------------
# F3+F5: outbound-answer commands (StartRecording conditional, Pause always for Bandwidth)
# ---------------------------------------------------------------------------------
@pytest.fixture
async def app_with_real_bandwidth_voice_and_mock_transport(engine):
    """A REAL Bandwidth adapter whose OUTBOUND create_call() HTTP request is answered by a
    MockTransport - this proves render_commands() genuinely produces the BXML the webhook
    response carries, not just what FakeVoiceCarrier claims it would."""
    from app.providers.bandwidth.adapter import BandwidthMessagingCarrier

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"callId": "bw-out-1"})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = make_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    carrier = BandwidthMessagingCarrier(
        account_id="acct-1",
        api_username="api-user",
        api_password="api-pass",
        application_id="msg-app-id",
        webhook_username=WEBHOOK_USER,
        webhook_password=WEBHOOK_PASS,
        client=mock_client,
    )
    carrier.voice_application_id = "voice-app-id"
    carrier.voice_callback_url = "https://example.test/api/v1/webhooks/bandwidth/voice"
    carrier.voice_webhook_username = WEBHOOK_USER
    carrier.voice_webhook_password = WEBHOOK_PASS
    install_voice_carrier(application, carrier)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, carrier, application, requests
    await mock_client.aclose()


async def test_outbound_answer_bxml_always_holds_the_line_open_with_pause(
    app_with_real_bandwidth_voice_and_mock_transport, session
):
    from tests.conftest import auth_headers, make_org_with_number

    client, _, _, _ = app_with_real_bandwidth_voice_and_mock_transport
    token, org, _ = await make_org_with_number(client, "outans1@example.com", "Org O", OUR)

    created = await client.post(
        "/api/v1/calls", json={"to": THEIRS}, headers=auth_headers(token, org["id"])
    )
    assert created.status_code == 201, created.text
    provider_id = created.json()["legs"][0]["provider_call_id"]
    assert provider_id == "bw-out-1"

    r = await _post(client, ANSWER_URL, _bw_event("answer", provider_id, to=THEIRS, from_=OUR))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/xml")
    assert "<Pause" in r.text
    assert "<StartRecording" not in r.text, "no `record` flag was set on this call"


async def test_outbound_answer_bxml_includes_start_recording_when_record_is_set(
    app_with_real_bandwidth_voice_and_mock_transport, session
):
    from tests.conftest import auth_headers, make_org_with_number

    client, _, _, _ = app_with_real_bandwidth_voice_and_mock_transport
    token, org, _ = await make_org_with_number(client, "outans2@example.com", "Org O", OUR)

    created = await client.post(
        "/api/v1/calls",
        json={"to": THEIRS, "record": True},
        headers=auth_headers(token, org["id"]),
    )
    assert created.status_code == 201, created.text
    provider_id = created.json()["legs"][0]["provider_call_id"]

    r = await _post(client, ANSWER_URL, _bw_event("answer", provider_id, to=THEIRS, from_=OUR))
    assert r.status_code == 200
    assert "<StartRecording" in r.text
    assert "<Pause" in r.text
    assert r.text.index("<StartRecording") < r.text.index("<Pause"), (
        "recording must start before the stopgap Pause, not after"
    )


# ==================================================================================
# Unit: BXML golden tests (Bandwidth command rendering)
# ==================================================================================
def _bandwidth_carrier():
    from app.providers.bandwidth.adapter import BandwidthMessagingCarrier

    return BandwidthMessagingCarrier(
        account_id="a", api_username="u", api_password="p", application_id="app"
    )


def test_bxml_speak_renders_exact_xml():
    from app.providers.voice import Speak as SpeakCmd

    xml = _bandwidth_carrier().render_commands([SpeakCmd(text="Hello")])
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        '<SpeakSentence voice="julie">Hello</SpeakSentence>'
        "</Response>"
    )


def test_bxml_escapes_special_characters_in_speak_text():
    from xml.sax.saxutils import escape

    from app.providers.voice import Speak as SpeakCmd

    text = 'Tom & Jerry <ran> "fast"'
    xml = _bandwidth_carrier().render_commands([SpeakCmd(text=text)])
    assert escape(text) in xml
    assert "<ran>" not in xml, "raw angle brackets must never reach the XML body"


def test_bxml_play():
    from app.providers.voice import Play

    xml = _bandwidth_carrier().render_commands([Play(url="https://x.example/a.mp3")])
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        "<PlayAudio>https://x.example/a.mp3</PlayAudio>"
        "</Response>"
    )


def test_bxml_gather_without_prompt():
    from app.providers.voice import Gather

    xml = _bandwidth_carrier().render_commands([Gather()])
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        '<Gather maxDigits="1" terminatingDigits="#" firstDigitTimeout="10"/>'
        "</Response>"
    )


def test_bxml_gather_with_speak_prompt_and_tag():
    from app.providers.voice import Gather
    from app.providers.voice import Speak as SpeakCmd

    xml = _bandwidth_carrier().render_commands(
        [
            Gather(
                max_digits=3,
                terminating_digit="*",
                timeout_seconds=5,
                prompt=SpeakCmd(text="Enter code"),
                action_tag="mytag",
            )
        ]
    )
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        '<Gather maxDigits="3" terminatingDigits="*" firstDigitTimeout="5" tag="mytag">'
        '<SpeakSentence voice="julie">Enter code</SpeakSentence>'
        "</Gather></Response>"
    )


def test_bxml_start_and_stop_recording():
    from app.providers.voice import StartRecording, StopRecording

    dual = _bandwidth_carrier().render_commands([StartRecording()])
    assert '<StartRecording fileFormat="mp3" multiChannel="true"/>' in dual

    single = _bandwidth_carrier().render_commands([StartRecording(channels="single")])
    assert '<StartRecording fileFormat="mp3"/>' in single
    assert "multiChannel" not in single

    stop = _bandwidth_carrier().render_commands([StopRecording()])
    assert "<StopRecording/>" in stop


def test_bxml_transfer():
    from app.providers.voice import Transfer

    xml = _bandwidth_carrier().render_commands([Transfer(to="+19725550199", from_="+12145550100")])
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        '<Transfer transferCallerId="+12145550100">'
        "<PhoneNumber>+19725550199</PhoneNumber></Transfer></Response>"
    )


def test_bxml_hangup_and_pause():
    xml = _bandwidth_carrier().render_commands([Hangup()])
    assert xml == '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'

    from app.providers.voice import Pause

    xml = _bandwidth_carrier().render_commands([Pause(seconds=2.5)])
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?><Response><Pause duration="2.5"/></Response>'
    )


def test_bxml_command_list_renders_in_order_inside_one_response():
    xml = _bandwidth_carrier().render_commands([Speak(text="hi"), Hangup()])
    assert xml.count("<Response>") == 1
    assert xml.count("</Response>") == 1
    assert xml.index("SpeakSentence") < xml.index("<Hangup/>")


def test_bxml_default_inbound_commands_render_speak_then_hangup():
    from app.api.routes.webhooks import DEFAULT_INBOUND_COMMANDS

    xml = _bandwidth_carrier().render_commands(DEFAULT_INBOUND_COMMANDS)
    assert xml.index("SpeakSentence") < xml.index("<Hangup/>")
    assert "not yet configured for inbound calls" in xml


def test_bxml_empty_command_list_is_an_empty_response():
    xml = _bandwidth_carrier().render_commands([])
    assert xml == '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


# ==================================================================================
# Unit: Telnyx executes commands as API actions
# ==================================================================================
async def test_telnyx_execute_commands_issues_api_actions_in_order():
    from app.providers.telnyx.adapter import TelnyxMessagingCarrier

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    carrier = TelnyxMessagingCarrier(api_key="test-key", client=client)
    try:
        await carrier.execute_commands("ccid-1", [Speak(text="hi there"), Hangup()])
    finally:
        await client.aclose()

    assert [str(r.url) for r in requests] == [
        "https://api.telnyx.com/v2/calls/ccid-1/actions/speak",
        "https://api.telnyx.com/v2/calls/ccid-1/actions/hangup",
    ]
    assert requests[0].headers["authorization"] == "Bearer test-key"
    body0 = json.loads(requests[0].content)
    assert body0 == {"payload": "hi there", "voice": "female", "language": "en-US"}
    assert json.loads(requests[1].content) == {}


async def test_telnyx_render_commands_is_none_commands_go_out_of_band():
    from app.providers.telnyx.adapter import TelnyxMessagingCarrier

    carrier = TelnyxMessagingCarrier(api_key="k")
    assert carrier.render_commands([Speak(text="x")]) is None


async def test_telnyx_bandwidth_recording_auth_never_attaches_credentials():
    """Telnyx hosts recordings publicly - see providers/telnyx/voice.py::recording_auth."""
    from app.providers.telnyx.adapter import TelnyxMessagingCarrier

    carrier = TelnyxMessagingCarrier(api_key="k")
    assert carrier.recording_auth("https://recordings.telnyx.com/x.mp3") is None
