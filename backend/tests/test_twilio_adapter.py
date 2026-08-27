"""Twilio adapter tests (phase-9b DR-2).

Twilio is the ORIGINAL Twilio-compatible API that `providers/signalwire/` was deliberately
shaped after - these tests exercise the Twilio package on its own terms (its own error
table, its own signature module, its own capabilities) rather than assuming SignalWire
coverage transfers, because DR-2 explicitly forbids sharing implementation between them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import urlencode

import httpx
import pytest

from app.errors import FeatureUnavailableError
from app.providers.domain import DeliveryReceipt, InboundMessage, OutboundMessage, UnknownEvent
from app.providers.numbers import NumberProvider, NumberSearch
from app.providers.twilio.adapter import TwilioMessagingCarrier
from app.providers.voice import (
    Gather,
    Hangup,
    Pause,
    Play,
    Speak,
    StartRecording,
    StopRecording,
    Transfer,
    VoiceCarrier,
)

ACCOUNT_SID = "AC00000000000000000000000000000000"
AUTH_TOKEN = "test-auth-token"
WEBHOOK_URL = "https://example.com/webhooks/twilio/sms"
VOICE_WEBHOOK_URL = "https://example.com/webhooks/twilio/voice"


def build(handler, **overrides) -> TwilioMessagingCarrier:
    kwargs = {
        "account_sid": ACCOUNT_SID,
        "auth_token": AUTH_TOKEN,
        "webhook_url": WEBHOOK_URL,
        "voice_webhook_url": VOICE_WEBHOOK_URL,
        "client": httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    }
    kwargs.update(overrides)
    return TwilioMessagingCarrier(**kwargs)


# ----------------------------------------------------------------------------------
# send_message
# ----------------------------------------------------------------------------------


async def test_send_success():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = dict(
            pair.split("=", 1) for pair in request.content.decode().split("&")
        )
        return httpx.Response(201, json={"sid": "SM123abc", "status": "queued"})

    carrier = build(handler)
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi", tag="corr-1")
    )

    assert result.status == "accepted"
    assert result.provider_message_id == "SM123abc"
    assert result.error is None
    assert captured["url"].endswith(f"/Accounts/{ACCOUNT_SID}/Messages.json")
    expected_auth = base64.b64encode(f"{ACCOUNT_SID}:{AUTH_TOKEN}".encode()).decode()
    assert captured["auth"] == f"Basic {expected_auth}"
    assert captured["body"]["To"] == "%2B19725550199"
    assert captured["body"]["From"] == "%2B12145550100"


async def test_send_uses_messaging_service_sid_instead_of_from():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "MessagingServiceSid=MG555" in body
        assert "From=" not in body
        return httpx.Response(200, json={"sid": "SM999"})

    carrier = build(handler, messaging_service_sid="MG555")
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi", tag="t")
    )
    assert result.status == "accepted"


async def test_send_repeats_media_url_per_item():
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert body.count("MediaUrl=") == 2
        return httpx.Response(201, json={"sid": "SM1"})

    carrier = build(handler)
    result = await carrier.send_message(
        OutboundMessage(
            to="+19725550199",
            from_="+12145550100",
            text="hi",
            media=("https://x/a.jpg", "https://x/b.jpg"),
        )
    )
    assert result.status == "accepted"


@pytest.mark.parametrize(
    "status,body,category,retryable,code",
    [
        (401, {"code": None, "message": "nope"}, "auth", False, None),
        (403, {"code": None, "message": "nope"}, "auth", False, None),
        (429, {"code": 20429, "message": "slow down"}, "rate_limited", True, "20429"),
        (400, {"code": 20429, "message": "slow down"}, "rate_limited", True, "20429"),
        (400, {"code": 30032, "message": "toll-free unverified"}, "unregistered", False, "30032"),
        (400, {"code": 30034, "message": "unregistered"}, "unregistered", False, "30034"),
        (400, {"code": 30038, "message": "unregistered"}, "unregistered", False, "30038"),
        (400, {"code": 21211, "message": "bad to"}, "invalid_request", False, "21211"),
        (400, {"code": 21606, "message": "bad from"}, "invalid_request", False, "21606"),
        (400, {"code": 99999, "message": "other"}, "invalid_request", False, "99999"),
        (503, {"code": None, "message": "upstream"}, "carrier_transient", True, None),
    ],
)
async def test_error_classification(status, body, category, retryable, code):
    carrier = build(lambda request: httpx.Response(status, json=body))
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi", tag="t")
    )
    assert result.status == "rejected"
    assert result.error is not None
    assert result.error.category == category
    assert result.error.retryable is retryable
    if code:
        assert result.error.carrier_code == code


async def test_21610_detail_names_the_carrier_side_opt_out():
    """21610 means the recipient replied STOP AT TWILIO - our own opt-out ledger may not
    know about it. The detail must say so explicitly, not read like an ordinary 400."""
    carrier = build(
        lambda request: httpx.Response(400, json={"code": 21610, "message": ""})
    )
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi", tag="t")
    )
    assert result.error.category == "invalid_request"
    assert result.error.retryable is False
    assert "opt-out" in result.error.detail.lower()


async def test_transport_error_is_unreachable_and_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    carrier = build(handler)
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi", tag="t")
    )
    assert result.status == "rejected"
    assert result.error.category == "carrier_unreachable"
    assert result.error.retryable is True


def test_capabilities_are_honest():
    caps = TwilioMessagingCarrier.capabilities
    assert caps.supports_cancel is True
    assert caps.supports_scheduled_send is True
    assert caps.sync_delivery_status is False
    assert caps.max_media_bytes == 5_000_000
    assert caps.group_mms_toll_free is False


# ----------------------------------------------------------------------------------
# media_auth - host-checked, never trusted from the payload
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expect_credentials",
    [
        ("https://api.twilio.com/2010-04-01/Accounts/AC1/Messages/SM1/Media/ME1", True),
        ("https://media.twilio.com/foo", True),
        ("https://nottwilio.com/foo", False),
        ("https://twilio.com.evil.net/foo", False),
        ("https://evil.com/foo", False),
    ],
)
def test_media_auth_host_checked(url, expect_credentials):
    carrier = build(lambda request: httpx.Response(200))
    result = carrier.media_auth(url)
    if expect_credentials:
        assert result == (ACCOUNT_SID, AUTH_TOKEN)
    else:
        assert result is None


# ----------------------------------------------------------------------------------
# webhook signature verification
# ----------------------------------------------------------------------------------


def _sign(url: str, params: dict, token: str) -> str:
    payload = url + "".join(f"{k}{v}" for k, v in sorted(params.items()))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def test_verify_webhook_valid_signature_passes():
    carrier = build(lambda request: httpx.Response(200))
    params = {"MessageSid": "SM1", "From": "+19725550199", "To": "+12145550100", "Body": "hi"}
    raw_body = urlencode(params).encode()
    sig = _sign(WEBHOOK_URL, params, AUTH_TOKEN)
    assert carrier.verify_webhook({"X-Twilio-Signature": sig}, raw_body) is True


def test_verify_webhook_tampered_body_fails():
    carrier = build(lambda request: httpx.Response(200))
    params = {"MessageSid": "SM1", "From": "+19725550199", "To": "+12145550100", "Body": "hi"}
    sig = _sign(WEBHOOK_URL, params, AUTH_TOKEN)
    tampered = urlencode({**params, "Body": "modified"}).encode()
    assert carrier.verify_webhook({"X-Twilio-Signature": sig}, tampered) is False


def test_verify_webhook_wrong_token_fails():
    carrier = build(lambda request: httpx.Response(200))
    params = {"MessageSid": "SM1", "From": "+19725550199", "To": "+12145550100", "Body": "hi"}
    raw_body = urlencode(params).encode()
    sig = _sign(WEBHOOK_URL, params, "a-different-token")
    assert carrier.verify_webhook({"X-Twilio-Signature": sig}, raw_body) is False


def test_verify_webhook_params_must_be_sorted():
    """The signature covers params in SORTED order regardless of how they arrive on the
    wire. A signature computed over the wire ORDER (not sorted) must not validate - proving
    the implementation actually sorts rather than accepting whatever order shows up."""
    carrier = build(lambda request: httpx.Response(200))
    # Deliberately out-of-alphabetical-order on the wire.
    wire_params = {"To": "+12145550100", "Body": "hi", "MessageSid": "SM1", "From": "+1972"}
    raw_body = urlencode(wire_params).encode()

    correct_sig = _sign(WEBHOOK_URL, wire_params, AUTH_TOKEN)
    assert carrier.verify_webhook({"X-Twilio-Signature": correct_sig}, raw_body) is True

    # A signature built by concatenating the UNSORTED wire order must be rejected.
    unsorted_payload = WEBHOOK_URL + "".join(f"{k}{v}" for k, v in wire_params.items())
    wrong_sig = base64.b64encode(
        hmac.new(AUTH_TOKEN.encode(), unsorted_payload.encode(), hashlib.sha1).digest()
    ).decode()
    assert wrong_sig != correct_sig
    assert carrier.verify_webhook({"X-Twilio-Signature": wrong_sig}, raw_body) is False


def test_verify_webhook_missing_header_fails():
    carrier = build(lambda request: httpx.Response(200))
    assert carrier.verify_webhook({}, b"MessageSid=SM1") is False


# ----------------------------------------------------------------------------------
# inbound parsing
# ----------------------------------------------------------------------------------


def test_parse_inbound_sms():
    carrier = build(lambda request: httpx.Response(200))
    body = urlencode(
        {
            "MessageSid": "SM1",
            "From": "+19725550199",
            "To": "+12145550100",
            "Body": "hello there",
            "NumMedia": "0",
        }
    ).encode()
    events = carrier.parse_webhook(body)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, InboundMessage)
    assert event.provider_message_id == "SM1"
    assert event.from_ == "+19725550199"
    assert event.to == "+12145550100"
    assert event.text == "hello there"
    assert event.media == ()


def test_parse_inbound_mms_two_media():
    carrier = build(lambda request: httpx.Response(200))
    body = urlencode(
        {
            "MessageSid": "MM1",
            "From": "+19725550199",
            "To": "+12145550100",
            "Body": "pic",
            "NumMedia": "2",
            "MediaUrl0": "https://api.twilio.com/media/0",
            "MediaContentType0": "image/jpeg",
            "MediaUrl1": "https://api.twilio.com/media/1",
            "MediaContentType1": "image/png",
        }
    ).encode()
    events = carrier.parse_webhook(body)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, InboundMessage)
    assert event.media == ("https://api.twilio.com/media/0", "https://api.twilio.com/media/1")


@pytest.mark.parametrize(
    "status,canonical",
    [
        ("queued", "message-sending"),
        ("sending", "message-sending"),
        ("sent", "message-sending"),
        ("delivered", "message-delivered"),
        ("undelivered", "message-failed"),
        ("failed", "message-failed"),
    ],
)
def test_parse_status_callback(status, canonical):
    carrier = build(lambda request: httpx.Response(200))
    body = urlencode({"MessageSid": "SM1", "MessageStatus": status}).encode()
    events = carrier.parse_webhook(body)
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, DeliveryReceipt)
    assert event.event_type == canonical
    assert event.provider_message_id == "SM1"


def test_parse_unknown_status_is_dead_lettered_not_guessed():
    carrier = build(lambda request: httpx.Response(200))
    body = urlencode({"MessageSid": "SM1", "MessageStatus": "some-new-status"}).encode()
    events = carrier.parse_webhook(body)
    assert len(events) == 1
    assert isinstance(events[0], UnknownEvent)


def test_parse_malformed_body_returns_empty_list():
    carrier = build(lambda request: httpx.Response(200))
    assert carrier.parse_webhook(b"") == []
    assert carrier.parse_webhook(b"not=a&valid=sms&payload=here") == []


# ----------------------------------------------------------------------------------
# numbers
# ----------------------------------------------------------------------------------


async def test_search_numbers_parses_available_numbers():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "available_phone_numbers": [
                    {
                        "phone_number": "+19725550100",
                        "region": "TX",
                        "locality": "Dallas",
                        "capabilities": {"voice": True, "SMS": True, "MMS": False},
                    }
                ]
            },
        )

    carrier = build(handler)
    results = await carrier.search_numbers(NumberSearch(area_code="972", number_type="local"))
    assert len(results) == 1
    number = results[0]
    assert number.e164 == "+19725550100"
    assert number.number_type == "local"
    assert number.region == "TX"
    assert number.locality == "Dallas"
    assert number.capabilities == {"sms": True, "mms": False, "voice": True}
    assert "AvailablePhoneNumbers/US/Local.json" in captured["url"]
    assert "AreaCode=972" in captured["url"]


async def test_search_numbers_tollfree_uses_tollfree_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "AvailablePhoneNumbers/US/TollFree.json" in str(request.url)
        return httpx.Response(200, json={"available_phone_numbers": []})

    carrier = build(handler)
    results = await carrier.search_numbers(NumberSearch(number_type="tollfree"))
    assert results == []


async def test_order_number_reports_active_and_provider_ref():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/IncomingPhoneNumbers.json")
        return httpx.Response(
            201,
            json={
                "sid": "PN123",
                "phone_number": "+19725550100",
                "capabilities": {"voice": True, "sms": True, "mms": True},
            },
        )

    carrier = build(handler)
    result = await carrier.order_number("+19725550100")
    assert result.status == "active"
    assert result.provider_ref == "PN123"
    assert result.e164 == "+19725550100"
    assert result.capabilities == {"sms": True, "mms": True, "voice": True}


async def test_release_number_calls_the_right_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(204)

    carrier = build(handler)
    await carrier.release_number("+19725550100", provider_ref="PN123")
    assert captured["method"] == "DELETE"
    assert captured["url"].endswith(f"/Accounts/{ACCOUNT_SID}/IncomingPhoneNumbers/PN123.json")


# ----------------------------------------------------------------------------------
# voice: TwiML rendering
# ----------------------------------------------------------------------------------


def _render(commands, **overrides):
    carrier = build(lambda request: httpx.Response(200), **overrides)
    return carrier.render_commands(commands)


def test_render_speak():
    xml = _render([Speak(text="Hello & welcome", voice="alice")])
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        '<Say voice="alice">Hello &amp; welcome</Say></Response>'
    )


def test_render_play():
    xml = _render([Play(url="https://example.com/a.mp3")])
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        "<Play>https://example.com/a.mp3</Play></Response>"
    )


def test_render_gather_with_speak_prompt_and_action():
    xml = _render(
        [
            Gather(
                max_digits=4,
                terminating_digit="#",
                timeout_seconds=8,
                prompt=Speak(text="Enter code", voice="alice"),
            )
        ]
    )
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Gather numDigits="4" finishOnKey="#" timeout="8" action="{VOICE_WEBHOOK_URL}">'
        '<Say voice="alice">Enter code</Say></Gather></Response>'
    )


def test_render_gather_without_prompt_self_closes():
    xml = _render([Gather(max_digits=1, terminating_digit="#", timeout_seconds=5)])
    assert "<Gather" in xml
    assert xml.count("<Gather") == 1
    assert "/>" in xml
    assert "</Gather>" not in xml


def test_render_start_recording():
    xml = _render([StartRecording()])
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?><Response><Record/></Response>'
    )


def test_render_stop_recording_omitted_no_twiml_verb():
    """TwiML has no stop-recording document verb; the command is dropped from the
    rendered document (not silently - the mixin logs a debug line when this happens)."""
    xml = _render([StopRecording()])
    assert xml == '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def test_render_transfer():
    xml = _render([Transfer(to="+19725551234", from_="+12145550100")])
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        '<Dial callerId="+12145550100"><Number>+19725551234</Number></Dial></Response>'
    )


def test_render_hangup():
    xml = _render([Hangup()])
    assert xml == '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


def test_render_pause():
    xml = _render([Pause(seconds=3)])
    assert xml == '<?xml version="1.0" encoding="UTF-8"?><Response><Pause length="3"/></Response>'


def test_render_ordered_multi_command_list():
    xml = _render([Speak(text="Hi"), Pause(seconds=1), Hangup()])
    assert xml == (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        '<Say voice="julie">Hi</Say><Pause length="1"/><Hangup/></Response>'
    )


def test_render_escapes_attribute_special_characters():
    """Attribute values go through xml.sax.saxutils.quoteattr (not the bare `escape()` used
    for element text) - assert against quoteattr's own output so this stays correct even
    though quoteattr picks its quote delimiter dynamically."""
    from xml.sax.saxutils import quoteattr

    raw_from = '"quoted" & tricky'
    xml = _render([Transfer(to="+1972 <b>", from_=raw_from)])
    expected = (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"<Dial callerId={quoteattr(raw_from)}>"
        "<Number>+1972 &lt;b&gt;</Number></Dial></Response>"
    )
    assert xml == expected
    # Sanity: the raw unescaped ampersand must never appear bare in the output.
    assert " & " not in xml


async def test_execute_commands_raises_feature_unavailable():
    carrier = build(lambda request: httpx.Response(200))
    with pytest.raises(FeatureUnavailableError):
        await carrier.execute_commands("CA123", [Hangup()])


# ----------------------------------------------------------------------------------
# voice: webhook parsing
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call_status,expected_event,expected_cause",
    [
        ("queued", "call_initiated", ""),
        ("ringing", "call_ringing", ""),
        ("in-progress", "call_answered", ""),
        ("completed", "call_hungup", ""),
        ("busy", "call_hungup", "busy"),
        ("failed", "call_hungup", "failed"),
        ("no-answer", "call_hungup", "no-answer"),
    ],
)
def test_parse_voice_webhook_call_status(call_status, expected_event, expected_cause):
    carrier = build(lambda request: httpx.Response(200))
    body = urlencode(
        {"CallSid": "CA1", "CallStatus": call_status, "To": "+1972", "From": "+1214"}
    ).encode()
    events = carrier.parse_voice_webhook(body)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == expected_event
    assert event.provider_call_id == "CA1"
    assert event.hangup_cause == expected_cause


@pytest.mark.parametrize(
    "answered_by,expected_event",
    [
        ("machine_start", "machine_detected"),
        ("machine_end_beep", "machine_detected"),
        ("human", "human_detected"),
    ],
)
def test_parse_voice_webhook_answered_by(answered_by, expected_event):
    carrier = build(lambda request: httpx.Response(200))
    body = urlencode({"CallSid": "CA1", "AnsweredBy": answered_by}).encode()
    events = carrier.parse_voice_webhook(body)
    assert len(events) == 1
    assert events[0].event_type == expected_event


def test_parse_voice_webhook_digits():
    carrier = build(lambda request: httpx.Response(200))
    body = urlencode({"CallSid": "CA1", "CallStatus": "in-progress", "Digits": "4321"}).encode()
    events = carrier.parse_voice_webhook(body)
    assert len(events) == 1
    assert events[0].event_type == "dtmf_received"
    assert events[0].digits == "4321"


def test_parse_voice_webhook_recording_ready():
    carrier = build(lambda request: httpx.Response(200))
    body = urlencode(
        {
            "CallSid": "CA1",
            "RecordingUrl": "https://api.twilio.com/recordings/RE1",
            "RecordingSid": "RE1",
        }
    ).encode()
    events = carrier.parse_voice_webhook(body)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "recording_ready"
    assert event.recording_url == "https://api.twilio.com/recordings/RE1"
    assert event.provider_recording_id == "RE1"


def test_parse_voice_webhook_duplicate_events_dedupe():
    carrier = build(lambda request: httpx.Response(200))
    body = urlencode(
        {
            "CallSid": "CA1",
            "CallStatus": "completed",
            "Timestamp": "Fri, 28 Aug 2026 12:00:00 +0000",
        }
    ).encode()
    first = carrier.parse_voice_webhook(body)
    second = carrier.parse_voice_webhook(body)
    assert first[0].provider_event_id == second[0].provider_event_id
    assert first[0].provider_event_id.startswith("twilio-voice-")


def test_parse_voice_webhook_malformed_returns_empty():
    carrier = build(lambda request: httpx.Response(200))
    assert carrier.parse_voice_webhook(b"") == []
    assert carrier.parse_voice_webhook(b"no_call_sid=here") == []


def test_recording_auth_host_checked():
    carrier = build(lambda request: httpx.Response(200))
    assert carrier.recording_auth("https://api.twilio.com/recordings/RE1") == (
        ACCOUNT_SID,
        AUTH_TOKEN,
    )
    assert carrier.recording_auth("https://evil.com/recordings/RE1") is None


# ----------------------------------------------------------------------------------
# protocol conformance
# ----------------------------------------------------------------------------------


def test_protocol_conformance():
    carrier = build(lambda request: httpx.Response(200))
    assert isinstance(carrier, NumberProvider)
    assert isinstance(carrier, VoiceCarrier)
