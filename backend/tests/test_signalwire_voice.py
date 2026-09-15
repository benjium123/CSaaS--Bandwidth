"""SignalWire voice over the Compatibility (LaML) API.

The two things worth defending here are the ones a live call would otherwise teach us the
hard way: the request SHAPE (form-encoded, Basic auth, the exact fields that opt a call into
answering-machine detection) and the webhook SIGNATURE, which must fail closed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qs, urlencode

import httpx
import pytest

from app.errors import FeatureUnavailableError
from app.providers.signalwire.adapter import SignalWireMessagingCarrier
from app.providers.voice import (
    Gather,
    Hangup,
    Pause,
    Play,
    Speak,
    StartRecording,
    Transfer,
    VoiceCarrier,
)

SPACE = "sabine.signalwire.com"
PROJECT = "9c2ec6d5-b851-4091-8b4b-c7ce0a845f87"
TOKEN = "swapi_test_token_not_a_real_one"
WEBHOOK = "https://csaas.example.test/api/v1/webhooks/signalwire/voice"
STATUS_CB = "https://csaas.example.test/api/v1/webhooks/signalwire/voice/status"


class FakeSignalWire:
    """Records every request and replays a scripted response."""

    def __init__(self, status: int = 201, payload: dict | None = None) -> None:
        self.status = status
        self.payload = payload if payload is not None else {"sid": "CA-123", "status": "queued"}
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self.status, json=self.payload)

        return httpx.MockTransport(handler)

    def form(self, index: int = 0) -> dict[str, list[str]]:
        return parse_qs(self.requests[index].content.decode())


def _carrier(fake: FakeSignalWire | None = None) -> SignalWireMessagingCarrier:
    client = httpx.AsyncClient(transport=(fake or FakeSignalWire()).transport())
    carrier = SignalWireMessagingCarrier(
        project_id=PROJECT, api_token=TOKEN, space_url=SPACE, client=client
    )
    carrier.voice_webhook_url = WEBHOOK
    carrier.voice_status_callback_url = STATUS_CB
    carrier.voice_signing_url = STATUS_CB
    return carrier


def _signed(params: dict[str, str], url: str = STATUS_CB) -> tuple[bytes, str]:
    """A body plus the signature SignalWire would send for it (Twilio's scheme)."""
    body = urlencode(params).encode()
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(TOKEN.encode(), payload.encode(), hashlib.sha1).digest()
    return body, base64.b64encode(digest).decode()


# ======================================================================================
# Capability
# ======================================================================================
def test_signalwire_declares_voice_support():
    """Capability is DECLARED by implementing the protocol, never discovered by a failing
    API call - so this is what makes SignalWire selectable for calling at all."""
    assert isinstance(_carrier(), VoiceCarrier)


# ======================================================================================
# create_call
# ======================================================================================
async def test_a_call_is_created_form_encoded_with_basic_auth():
    fake = FakeSignalWire()
    carrier = _carrier(fake)

    result = await carrier.create_call(to="+16824231003", from_="+12145550100", tag="t-1")

    assert result.status == "accepted"
    assert result.provider_call_id == "CA-123"
    request = fake.requests[0]
    assert request.url.path.endswith(f"/Accounts/{PROJECT}/Calls.json")
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    expected = base64.b64encode(f"{PROJECT}:{TOKEN}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    form = fake.form()
    assert form["To"] == ["+16824231003"]
    assert form["From"] == ["+12145550100"]
    assert form["Url"] == [WEBHOOK]
    # No query string on a signed URL: SignalWire signs the URL it called, and verification
    # only knows the CONFIGURED one, so a "?tag=" would make every callback fail closed.
    assert form["StatusCallback"] == [STATUS_CB]


async def test_machine_detection_is_only_requested_when_asked_for():
    """Every AMD-enabled call pays detection latency before it connects, so the field must
    never be sent by default."""
    off = FakeSignalWire()
    await _carrier(off).create_call(to="+16824231003", from_="+12145550100")
    assert "MachineDetection" not in off.form()

    on = FakeSignalWire()
    await _carrier(on).create_call(
        to="+16824231003", from_="+12145550100", machine_detection="async"
    )
    assert on.form()["MachineDetection"] == ["Enable"]


async def test_a_refused_call_is_reported_not_raised():
    """A carrier saying no is a routing decision the failover walk has to read, not an
    exception that unwinds the dial."""
    fake = FakeSignalWire(status=401, payload={"code": "20003", "message": "Authenticate"})

    result = await _carrier(fake).create_call(to="+16824231003", from_="+12145550100")

    assert result.status == "rejected"
    assert result.error is not None
    assert result.error.category == "auth"
    assert result.error.retryable is False


async def test_an_unreachable_carrier_is_rejected_as_retryable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    carrier = SignalWireMessagingCarrier(
        project_id=PROJECT, api_token=TOKEN, space_url=SPACE, client=client
    )
    carrier.voice_webhook_url = WEBHOOK

    result = await carrier.create_call(to="+16824231003", from_="+12145550100")

    assert result.status == "rejected"
    assert result.error is not None and result.error.retryable is True


# ======================================================================================
# LaML rendering
# ======================================================================================
def test_speech_is_xml_escaped():
    """A customer's own words end up inside the document. An unescaped ampersand does not
    mis-render, it makes SignalWire reject the whole document mid-call."""
    xml = _carrier().render_commands([Speak(text="Bell & Co <today>")])

    assert "Bell &amp; Co &lt;today&gt;" in xml
    assert "<today>" not in xml


def test_a_gather_nests_its_prompt_so_a_keypress_can_barge_in():
    xml = _carrier().render_commands(
        [Gather(max_digits=3, timeout_seconds=7, prompt=Speak(text="Press one"), action_tag="g1")]
    )

    assert "<Gather" in xml and 'numDigits="3"' in xml and 'timeout="7"' in xml
    # The prompt lives INSIDE the Gather, not before it.
    assert xml.index("<Gather") < xml.index("<Say") < xml.index("</Gather>")
    # The action URL stays bare for the same signing reason as the status callback.
    assert f'action="{WEBHOOK}"' in xml


def test_the_default_voice_is_translated_to_one_laml_knows():
    """Our neutral default is Plivo's name for a voice. Sent verbatim, SignalWire rejects the
    document at the moment the call connects."""
    xml = _carrier().render_commands([Speak(text="hi")])

    assert "julie" not in xml
    assert 'voice="alice"' in xml


def test_a_sub_second_pause_rounds_up_because_laml_rejects_zero():
    assert 'length="1"' in _carrier().render_commands([Pause(seconds=0.2)])


def test_hangup_transfer_and_recording_render_their_verbs():
    xml = _carrier().render_commands(
        [StartRecording(), Transfer(to="+19725550100", from_="+12145550100"), Hangup()]
    )

    assert "<Record " in xml
    assert "<Dial" in xml and "<Number>+19725550100</Number>" in xml
    assert "<Hangup/>" in xml
    assert xml.startswith('<?xml version="1.0" encoding="utf-8"?><Response>')
    assert xml.endswith("</Response>")


def test_a_play_url_is_rendered():
    assert "<Play>https://cdn.test/a.mp3</Play>" in _carrier().render_commands(
        [Play(url="https://cdn.test/a.mp3")]
    )


# ======================================================================================
# Mid-call control
# ======================================================================================
async def test_hangup_ends_the_live_call():
    fake = FakeSignalWire(status=200, payload={"sid": "CA-123", "status": "completed"})

    await _carrier(fake).execute_commands("CA-123", [Hangup()])

    assert fake.requests[0].url.path.endswith("/Calls/CA-123.json")
    assert fake.form()["Status"] == ["completed"]


async def test_speaking_mid_call_is_refused_in_plain_words():
    """SignalWire has no live command channel. Pretending otherwise would fail silently at
    the worst possible moment - mid-call."""
    with pytest.raises(FeatureUnavailableError):
        await _carrier().execute_commands("CA-123", [Speak(text="hello")])


async def test_a_refused_control_call_raises_something_catchable():
    """Regression: the failure description is a dataclass, not an exception - raising it
    directly would blow up with "exceptions must derive from BaseException"."""
    fake = FakeSignalWire(status=404, payload={"code": "20404", "message": "gone"})

    with pytest.raises(FeatureUnavailableError):
        await _carrier(fake).execute_commands("CA-123", [Hangup()])


# ======================================================================================
# Webhook verification - fails closed
# ======================================================================================
def test_a_correctly_signed_webhook_is_accepted():
    body, signature = _signed({"CallSid": "CA-123", "CallStatus": "completed"})

    assert _carrier().verify_voice_webhook({"X-Twilio-Signature": signature}, body) is True


def test_a_tampered_body_is_rejected():
    body, signature = _signed({"CallSid": "CA-123", "CallStatus": "completed"})
    tampered = body.replace(b"completed", b"no-answer")

    assert _carrier().verify_voice_webhook({"X-Twilio-Signature": signature}, tampered) is False


def test_a_missing_signature_is_rejected():
    body, _sig = _signed({"CallSid": "CA-123"})

    assert _carrier().verify_voice_webhook({}, body) is False


def test_an_unconfigured_signing_url_verifies_nothing():
    """With nothing to sign against, anyone who knows the webhook path could drive calls.
    Fail closed."""
    carrier = _carrier()
    carrier.voice_signing_url = ""
    body, signature = _signed({"CallSid": "CA-123"})

    assert carrier.verify_voice_webhook({"X-Twilio-Signature": signature}, body) is False


# ======================================================================================
# Webhook parsing
# ======================================================================================
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ringing", "call_ringing"),
        ("in-progress", "call_answered"),
        ("completed", "call_hungup"),
        ("no-answer", "call_hungup"),
    ],
)
def test_call_status_maps_into_our_own_vocabulary(status, expected):
    body = urlencode({"CallSid": "CA-1", "CallStatus": status}).encode()

    events = _carrier().parse_voice_webhook(body)

    assert [e.event_type for e in events] == [expected]
    assert events[0].provider_call_id == "CA-1"


def test_a_hangup_carries_the_carriers_own_reason():
    body = urlencode({"CallSid": "CA-1", "CallStatus": "busy"}).encode()

    assert _carrier().parse_voice_webhook(body)[0].hangup_cause == "busy"


@pytest.mark.parametrize(
    ("answered_by", "expected"),
    [("machine_start", "machine_detected"), ("human", "human_detected")],
)
def test_answering_machine_detection_uses_the_canonical_names(answered_by, expected):
    """Regression: an invented event name would have been dropped as unknown, silently
    losing every AMD result on this carrier."""
    body = urlencode(
        {"CallSid": "CA-1", "CallStatus": "in-progress", "AnsweredBy": answered_by}
    ).encode()

    assert [e.event_type for e in _carrier().parse_voice_webhook(body)] == [expected]


def test_digits_and_recordings_are_recognised():
    digits = urlencode({"CallSid": "CA-1", "CallStatus": "in-progress", "Digits": "42"}).encode()
    events = _carrier().parse_voice_webhook(digits)
    assert events[0].event_type == "dtmf_received" and events[0].digits == "42"

    rec = urlencode(
        {
            "CallSid": "CA-1",
            "RecordingUrl": f"https://{SPACE}/rec/RE1",
            "RecordingSid": "RE1",
            "RecordingDuration": "12",
        }
    ).encode()
    events = _carrier().parse_voice_webhook(rec)
    assert events[0].event_type == "recording_ready"
    assert events[0].provider_recording_id == "RE1"
    assert events[0].duration_seconds == 12


def test_a_callback_with_no_leg_id_is_dropped():
    assert _carrier().parse_voice_webhook(urlencode({"CallStatus": "completed"}).encode()) == []


# ======================================================================================
# Recording credentials
# ======================================================================================
def test_recording_credentials_go_only_to_our_own_space():
    """A recording URL arrives inside an attacker-influenceable payload. Handing our project
    token to whatever host it names would give it away."""
    carrier = _carrier()

    assert carrier.recording_auth(f"https://{SPACE}/rec/RE1") == (PROJECT, TOKEN)
    assert carrier.recording_auth("https://evil.example/rec/RE1") is None
    assert carrier.recording_auth("https://evil.signalwire.com/rec/RE1") is None
