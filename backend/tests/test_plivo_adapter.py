"""Plivo adapter: messaging, V3 signature, numbers, voice.

Plivo is the CAL's proof carrier (phase-9b DR-3) - the first carrier that is not
Bandwidth-, Telnyx- or Twilio-shaped. These tests exercise every place that shows up:
Basic auth-id/auth-token, message_uuid returned as a LIST, powerpack pooled sending,
prose-only error classification, the V3 url+nonce signature (with its
comma-separated-candidates quirk), form-encoded webhooks, and Plivo XML rendering.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from urllib.parse import urlencode

import httpx
import pytest

from app.errors import FeatureUnavailableError
from app.providers.domain import OutboundMessage
from app.providers.numbers import NumberProvider, NumberSearch
from app.providers.plivo import webhooks as pl_webhooks
from app.providers.plivo.adapter import PlivoMessagingCarrier
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

AUTH_ID = "MAXXXXXXXXXXXXXXXXXX"
AUTH_TOKEN = "supersecrettoken"
WEBHOOK_URL = "https://csaas.example.com/webhooks/plivo/messages"


def build(handler, **kwargs) -> PlivoMessagingCarrier:
    return PlivoMessagingCarrier(
        auth_id=AUTH_ID,
        auth_token=AUTH_TOKEN,
        webhook_url=WEBHOOK_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def form_body(fields: dict[str, str]) -> bytes:
    return urlencode(fields).encode("utf-8")


def sign(url: str, nonce: str, token: str = AUTH_TOKEN) -> str:
    mac = hmac.new(token.encode(), (url + nonce).encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


# ==================================================================================
# send_message
# ==================================================================================


async def test_send_success():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(202, json={"message_uuid": ["msg-uuid-1"], "message": "accepted"})

    carrier = build(handler)
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi", tag="corr-1")
    )

    assert result.status == "accepted"
    # Plivo's real shape: message_uuid is a LIST even for a single recipient.
    assert result.provider_message_id == "msg-uuid-1"
    assert result.error is None

    assert captured["url"].endswith(f"/Account/{AUTH_ID}/Message/")
    expected_auth = base64.b64encode(f"{AUTH_ID}:{AUTH_TOKEN}".encode()).decode()
    assert captured["auth"] == f"Basic {expected_auth}"
    assert captured["body"]["src"] == "+12145550100"
    assert captured["body"]["dst"] == "+19725550199"
    assert captured["body"]["type"] == "sms"
    assert "powerpack_uuid" not in captured["body"]


async def test_send_success_message_uuid_as_bare_string():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"message_uuid": "msg-uuid-2"})

    carrier = build(handler)
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi")
    )
    assert result.status == "accepted"
    assert result.provider_message_id == "msg-uuid-2"


async def test_send_with_media_sets_mms_type():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(202, json={"message_uuid": ["m"]})

    carrier = build(handler)
    await carrier.send_message(
        OutboundMessage(
            to="+19725550199",
            from_="+12145550100",
            text="pic",
            media=("https://example.com/a.jpg",),
        )
    )
    assert captured["body"]["type"] == "mms"
    assert captured["body"]["media_urls"] == ["https://example.com/a.jpg"]


async def test_powerpack_path_sends_powerpack_uuid_not_src():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(202, json={"message_uuid": ["m"]})

    carrier = build(handler, powerpack_uuid="pp-uuid-1")
    await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi")
    )
    assert captured["body"]["powerpack_uuid"] == "pp-uuid-1"
    assert "src" not in captured["body"]


@pytest.mark.parametrize(
    "status,body,category,retryable",
    [
        (401, {"error": "Invalid credentials"}, "auth", False),
        (403, {"error": "Forbidden"}, "auth", False),
        (429, {"error": "Too many requests"}, "rate_limited", True),
        (500, {"error": "Internal error"}, "carrier_transient", True),
        (400, {"error": "Invalid destination number format"}, "invalid_request", False),
        (404, {"error": "Not found"}, "invalid_request", False),
        (422, {"error": "Unprocessable"}, "invalid_request", False),
    ],
)
async def test_error_classification(status, body, category, retryable):
    carrier = build(lambda request: httpx.Response(status, json=body))
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi")
    )
    assert result.status == "rejected"
    assert result.error.category == category
    assert result.error.retryable is retryable
    assert result.error.carrier_code is None  # Plivo never gives us a code


@pytest.mark.parametrize(
    "message",
    [
        "This number is not registered for a 10DLC campaign",
        "Sender is unregistered for messaging",
        "No campaign associated with this number",
    ],
)
async def test_prose_unregistered_detection(message):
    carrier = build(lambda request: httpx.Response(400, json={"error": message}))
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi")
    )
    assert result.status == "rejected"
    assert result.error.category == "unregistered"
    assert result.error.retryable is False


async def test_transport_error_is_unreachable_and_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    carrier = build(handler)
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi")
    )
    assert result.error.category == "carrier_unreachable"
    assert result.error.retryable is True


def test_capabilities_are_honest():
    caps = PlivoMessagingCarrier.capabilities
    assert caps.supports_cancel is False
    assert caps.supports_scheduled_send is False
    assert caps.sync_delivery_status is False
    assert caps.max_media_bytes == 5_000_000
    assert caps.group_mms_toll_free is False


# ==================================================================================
# media_auth
# ==================================================================================


def test_media_auth_plivo_host():
    carrier = build(lambda r: httpx.Response(200))
    assert carrier.media_auth("https://media.plivo.com/v1/file.jpg") == (AUTH_ID, AUTH_TOKEN)
    assert carrier.media_auth("https://plivo.com/file.jpg") == (AUTH_ID, AUTH_TOKEN)


@pytest.mark.parametrize(
    "url",
    [
        "https://notplivo.com/file.jpg",
        "https://plivo.com.evil.net/file.jpg",
        "https://example.com/file.jpg",
    ],
)
def test_media_auth_rejects_lookalike_hosts(url):
    carrier = build(lambda r: httpx.Response(200))
    assert carrier.media_auth(url) is None


# ==================================================================================
# V3 signature verification
# ==================================================================================


def test_v3_signature_valid_passes():
    nonce = "nonce-123"
    signature = sign(WEBHOOK_URL, nonce)
    headers = {"X-Plivo-Signature-V3": signature, "X-Plivo-Signature-V3-Nonce": nonce}
    assert pl_webhooks.verify(headers, AUTH_TOKEN, WEBHOOK_URL) is True


def test_v3_signature_tampered_url_fails():
    nonce = "nonce-123"
    signature = sign(WEBHOOK_URL, nonce)
    headers = {"X-Plivo-Signature-V3": signature, "X-Plivo-Signature-V3-Nonce": nonce}
    assert pl_webhooks.verify(headers, AUTH_TOKEN, WEBHOOK_URL + "/tampered") is False


def test_v3_signature_wrong_token_fails():
    nonce = "nonce-123"
    signature = sign(WEBHOOK_URL, nonce)
    headers = {"X-Plivo-Signature-V3": signature, "X-Plivo-Signature-V3-Nonce": nonce}
    assert pl_webhooks.verify(headers, "wrong-token", WEBHOOK_URL) is False


def test_v3_signature_wrong_nonce_fails():
    signature = sign(WEBHOOK_URL, "nonce-123")
    headers = {"X-Plivo-Signature-V3": signature, "X-Plivo-Signature-V3-Nonce": "nonce-456"}
    assert pl_webhooks.verify(headers, AUTH_TOKEN, WEBHOOK_URL) is False


def test_v3_signature_multiple_candidates_second_valid_passes():
    nonce = "nonce-123"
    valid = sign(WEBHOOK_URL, nonce)
    invalid = sign(WEBHOOK_URL, nonce, token="not-the-right-token")
    headers = {
        "X-Plivo-Signature-V3": f"{invalid},{valid}",
        "X-Plivo-Signature-V3-Nonce": nonce,
    }
    assert pl_webhooks.verify(headers, AUTH_TOKEN, WEBHOOK_URL) is True


def test_v3_signature_missing_headers_fails():
    assert pl_webhooks.verify({}, AUTH_TOKEN, WEBHOOK_URL) is False


# ==================================================================================
# Inbound message parsing
# ==================================================================================


def test_parse_inbound_sms():
    body = form_body(
        {
            "MessageUUID": "in-msg-1",
            "From": "19725550199",
            "To": "12145550100",
            "Text": "hello there",
        }
    )
    events = pl_webhooks.parse(body)
    assert len(events) == 1
    event = events[0]
    assert event.provider_message_id == "in-msg-1"
    assert event.from_ == "19725550199"
    assert event.to == "12145550100"
    assert event.text == "hello there"
    assert event.media == ()


def test_parse_inbound_mms_media_indexed_keys():
    body = form_body(
        {
            "MessageUUID": "in-msg-2",
            "From": "19725550199",
            "To": "12145550100",
            "Text": "pic",
            "Media0": "https://media.plivo.com/a.jpg",
            "Media1": "https://media.plivo.com/b.jpg",
        }
    )
    events = pl_webhooks.parse(body)
    assert events[0].media == (
        "https://media.plivo.com/a.jpg",
        "https://media.plivo.com/b.jpg",
    )


def test_parse_inbound_mms_media_urls_json_list():
    body = form_body(
        {
            "MessageUUID": "in-msg-3",
            "From": "19725550199",
            "To": "12145550100",
            "Text": "pic",
            "MediaUrls": json.dumps(["https://media.plivo.com/a.jpg"]),
        }
    )
    events = pl_webhooks.parse(body)
    assert events[0].media == ("https://media.plivo.com/a.jpg",)


@pytest.mark.parametrize(
    "status,canonical",
    [
        ("queued", "message-sending"),
        ("sent", "message-sending"),
        ("delivered", "message-delivered"),
        ("undelivered", "message-failed"),
        ("failed", "message-failed"),
        ("rejected", "message-failed"),
    ],
)
def test_parse_delivery_status_mapping(status, canonical):
    body = form_body({"MessageUUID": "dlr-1", "Status": status, "To": "12145550100"})
    events = pl_webhooks.parse(body)
    assert len(events) == 1
    assert events[0].event_type == canonical
    assert events[0].provider_message_id == "dlr-1"


def test_parse_delivery_unknown_status_skips():
    body = form_body({"MessageUUID": "dlr-2", "Status": "some-new-status"})
    assert pl_webhooks.parse(body) == []


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not=validutf8\xff",
        b"NoMessageUUIDHere=1",
    ],
)
def test_parse_malformed_returns_empty_list(raw):
    assert pl_webhooks.parse(raw) == []


# ==================================================================================
# Numbers
# ==================================================================================


async def test_search_numbers_hits_right_url_and_parses():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "objects": [
                    {
                        "number": "12145550111",
                        "region": "Texas, United States",
                        "monthly_rental_rate": "0.80",
                        "sms_enabled": True,
                        "mms_enabled": True,
                        "voice_enabled": True,
                    }
                ]
            },
        )

    carrier = build(handler)
    results = await carrier.search_numbers(NumberSearch(area_code="214", number_type="local"))
    assert "/PhoneNumber/" in captured["url"]
    assert "country_iso=US" in captured["url"]
    assert "type=local" in captured["url"]
    assert len(results) == 1
    assert results[0].e164 == "+12145550111"
    assert results[0].region == "Texas, United States"
    assert results[0].monthly_cost == "0.80"
    assert results[0].capabilities == {"sms": True, "mms": True, "voice": True}


async def test_order_number_hits_right_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        return httpx.Response(201, json={"apiId": "api-order-1", "message": "created"})

    carrier = build(handler)
    result = await carrier.order_number("+12145550111")
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/PhoneNumber/12145550111/")
    assert result.status == "active"
    assert result.e164 == "+12145550111"
    assert result.provider_ref == "api-order-1"


async def test_release_number_hits_right_url():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        return httpx.Response(204)

    carrier = build(handler)
    await carrier.release_number("+12145550111")
    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/Number/12145550111/")


# ==================================================================================
# Voice
# ==================================================================================


def test_render_speak_and_play():
    carrier = build(lambda r: httpx.Response(200))
    xml = carrier.render_commands([Speak(text="Hi & welcome"), Play(url="https://x.com/a.mp3")])
    assert xml == (
        '<?xml version="1.0" encoding="utf-8"?><Response>'
        "<Speak>Hi &amp; welcome</Speak>"
        "<Play>https://x.com/a.mp3</Play>"
        "</Response>"
    )


def test_render_gather_with_prompt_and_ordered_commands():
    carrier = build(lambda r: httpx.Response(200))
    xml = carrier.render_commands(
        [
            Gather(
                max_digits=4,
                terminating_digit="#",
                timeout_seconds=8,
                prompt=Speak(text="Enter your PIN"),
                action_tag="pin-gather",
            ),
            Hangup(),
        ]
    )
    assert xml.startswith('<?xml version="1.0" encoding="utf-8"?><Response>')
    assert xml.endswith("</Response>")
    assert '<GetDigits numDigits="4"' in xml
    assert 'finishOnKey="#"' in xml
    assert 'timeout="8"' in xml
    assert f"action=\"{WEBHOOK_URL}/gather?tag=pin-gather\"" in xml
    assert "<Speak>Enter your PIN</Speak></GetDigits>" in xml
    # ordering preserved: Gather THEN Hangup
    assert xml.index("</GetDigits>") < xml.index("<Hangup/>")


def test_render_recording_transfer_pause_and_escaping():
    carrier = build(lambda r: httpx.Response(200))
    xml = carrier.render_commands(
        [
            StartRecording(),
            Transfer(to="+19725550199", from_='Bob "Sales" <boss>'),
            Pause(seconds=3),
        ]
    )
    assert "<Record/>" in xml
    # xml.sax.saxutils.quoteattr picks single quotes when the value contains a double
    # quote but no single quote - still correctly escaped XML.
    assert "<Dial callerId='Bob \"Sales\" &lt;boss&gt;'>" in xml
    assert "<Number>+19725550199</Number></Dial>" in xml
    assert '<Wait length="3"/>' in xml


def test_render_stop_recording_omitted():
    carrier = build(lambda r: httpx.Response(200))
    xml = carrier.render_commands([StartRecording(), StopRecording(), Hangup()])
    assert "StopRecording" not in xml
    assert xml == (
        '<?xml version="1.0" encoding="utf-8"?><Response>'
        "<Record/>"
        "<Hangup/>"
        "</Response>"
    )


async def test_execute_commands_raises_feature_unavailable():
    carrier = build(lambda r: httpx.Response(200))
    with pytest.raises(FeatureUnavailableError):
        await carrier.execute_commands("call-uuid-1", [Hangup()])


def test_parse_voice_webhook_status_mapping():
    carrier = build(lambda r: httpx.Response(200))
    body = form_body(
        {
            "CallUUID": "call-1",
            "CallStatus": "in-progress",
            "To": "12145550100",
            "From": "19725550199",
        }
    )
    events = carrier.parse_voice_webhook(body)
    assert len(events) == 1
    assert events[0].event_type == "call_answered"
    assert events[0].provider_call_id == "call-1"


def test_parse_voice_webhook_digits():
    carrier = build(lambda r: httpx.Response(200))
    body = form_body({"CallUUID": "call-2", "Digits": "1234"})
    events = carrier.parse_voice_webhook(body)
    assert events[0].event_type == "dtmf_received"
    assert events[0].digits == "1234"


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("MachineDetection", "true", "machine_detected"),
        ("MachineDetection", "MACHINE", "machine_detected"),
        ("Machine", "false", "human_detected"),
    ],
)
def test_parse_voice_webhook_machine_detection(field, value, expected):
    carrier = build(lambda r: httpx.Response(200))
    body = form_body({"CallUUID": "call-3", field: value})
    events = carrier.parse_voice_webhook(body)
    assert events[0].event_type == expected


def test_parse_voice_webhook_recording_ready():
    carrier = build(lambda r: httpx.Response(200))
    body = form_body(
        {
            "CallUUID": "call-4",
            "RecordUrl": "https://media.plivo.com/rec.mp3",
            "RecordingID": "rec-1",
        }
    )
    events = carrier.parse_voice_webhook(body)
    assert events[0].event_type == "recording_ready"
    assert events[0].recording_url == "https://media.plivo.com/rec.mp3"
    assert events[0].provider_recording_id == "rec-1"


def test_parse_voice_webhook_duplicate_events_have_identical_provider_event_id():
    carrier = build(lambda r: httpx.Response(200))
    body = form_body({"CallUUID": "call-5", "CallStatus": "completed"})
    first = carrier.parse_voice_webhook(body)
    second = carrier.parse_voice_webhook(body)
    assert first[0].provider_event_id == second[0].provider_event_id
    assert first[0].provider_event_id.startswith("plivo-voice-")


def test_parse_voice_webhook_unknown_event_returns_empty():
    carrier = build(lambda r: httpx.Response(200))
    body = form_body({"CallUUID": "call-6", "CallStatus": "some-unknown-status"})
    assert carrier.parse_voice_webhook(body) == []


def test_recording_auth_mirrors_media_auth():
    carrier = build(lambda r: httpx.Response(200))
    assert carrier.recording_auth("https://media.plivo.com/rec.mp3") == (AUTH_ID, AUTH_TOKEN)
    assert carrier.recording_auth("https://notplivo.com/rec.mp3") is None


# ==================================================================================
# Protocol conformance
# ==================================================================================


def test_protocol_conformance():
    carrier = build(lambda r: httpx.Response(200))
    assert isinstance(carrier, NumberProvider)
    assert isinstance(carrier, VoiceCarrier)
