from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.providers.bandwidth.adapter import BandwidthMessagingCarrier
from app.providers.domain import OutboundMessage
from tests.conftest import load_fixture

ACCOUNT = "acct-12345"
APP_ID = "93de2206-9669-4e07-948d-329f4b722ee2"


def build(handler) -> BandwidthMessagingCarrier:
    return BandwidthMessagingCarrier(
        account_id=ACCOUNT,
        api_username="apiuser",
        api_password="apipass",
        application_id=APP_ID,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_send_success():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(202, json=load_fixture("send-202.json"))

    carrier = build(handler)
    result = await carrier.send_message(
        OutboundMessage(to="+19725550199", from_="+12145550100", text="hi", tag="corr-1")
    )

    assert result.status == "accepted"
    assert result.provider_message_id == "1755000000000-outbound-bbbb"
    assert result.error is None

    assert ACCOUNT in captured["url"]
    assert captured["url"].endswith(f"/users/{ACCOUNT}/messages")
    expected = base64.b64encode(b"apiuser:apipass").decode()
    assert captured["auth"] == f"Basic {expected}"
    # Bandwidth wants `to` as a LIST even for a single recipient.
    assert captured["body"]["to"] == ["+19725550199"]
    assert captured["body"]["applicationId"] == APP_ID
    assert captured["body"]["tag"] == "corr-1"


@pytest.mark.parametrize(
    "status,body,category,retryable,code",
    [
        (400, {"type": "4720", "description": "bad dest"}, "invalid_request", False, "4720"),
        (400, {"type": "4302", "description": "bad from"}, "invalid_request", False, "4302"),
        (400, {"type": "4476", "description": "unregistered"}, "unregistered", False, "4476"),
        (429, {"type": "4780", "description": "slow down"}, "rate_limited", True, "4780"),
        (503, {"description": "upstream"}, "carrier_transient", True, None),
        (401, {"description": "nope"}, "auth", False, None),
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
    """The adapter must not claim abilities Bandwidth does not have.

    Capabilities are per-INSTANCE, not per-class: whether this deployment may send
    messages depends on whether it was given a messaging application id, and a
    voice-only Bandwidth account is a real, supported configuration.
    """
    caps = BandwidthMessagingCarrier(
        account_id="a", api_username="u", api_password="p", application_id="msg-app"
    ).capabilities
    assert caps.supports_cancel is False
    assert caps.supports_scheduled_send is False
    # 202 Accepted is not delivery. Nothing may pretend otherwise.
    assert caps.sync_delivery_status is False
    assert caps.max_media_bytes == 3_750_000
    assert caps.supports_messaging is True


def test_a_voice_only_account_does_not_claim_messaging():
    """No messaging application id means Bandwidth will not carry texts for this account.
    Saying so up front beats finding out from a rejected send."""
    caps = BandwidthMessagingCarrier(
        account_id="a", api_username="u", api_password="p", application_id=""
    ).capabilities
    assert caps.supports_messaging is False
