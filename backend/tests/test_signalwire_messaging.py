"""P39: SignalWire texting must ask for delivery receipts, and we must parse them."""

from __future__ import annotations

from urllib.parse import parse_qs, urlencode

import httpx

from app.providers.domain import OutboundMessage
from app.providers.signalwire.adapter import SignalWireMessagingCarrier

_WEBHOOK = "https://csaas.example.test/api/v1/webhooks/signalwire/messaging"


class FakeSignalWire:
    def __init__(self, status=201, payload=None):
        self.status = status
        self.payload = payload if payload is not None else {"sid": "SM-1", "status": "queued"}
        self.requests: list[httpx.Request] = []

    def transport(self):
        def handler(request):
            self.requests.append(request)
            return httpx.Response(self.status, json=self.payload)

        return httpx.MockTransport(handler)

    def form(self, i=0):
        return parse_qs(self.requests[i].content.decode())


def _carrier(fake, webhook_url=_WEBHOOK):
    return SignalWireMessagingCarrier(
        project_id="p",
        api_token="t",
        space_url="sabine.signalwire.com",
        webhook_url=webhook_url,
        client=httpx.AsyncClient(transport=fake.transport()),
    )


async def test_send_requests_a_delivery_receipt():
    fake = FakeSignalWire()
    carrier = _carrier(fake)

    await carrier.send_message(
        OutboundMessage(to="+14155552222", from_="+14155551111", text="hello", media=())
    )

    assert fake.form()["StatusCallback"] == [_WEBHOOK]


async def test_no_receipt_is_requested_without_a_webhook_url():
    fake = FakeSignalWire()
    carrier = _carrier(fake, webhook_url="")

    await carrier.send_message(
        OutboundMessage(to="+14155552222", from_="+14155551111", text="hello", media=())
    )

    assert "StatusCallback" not in fake.form()


async def test_delivery_receipt_is_parsed():
    from app.providers.signalwire import webhooks

    def _body(status):
        return urlencode(
            {
                "MessageSid": "SM-1",
                "MessageStatus": status,
                "To": "+14155552222",
                "From": "+14155551111",
            }
        ).encode()

    delivered = webhooks.parse(_body("delivered"))
    assert len(delivered) == 1
    assert delivered[0].event_type == "message-delivered"

    failed = webhooks.parse(_body("undelivered"))
    assert len(failed) == 1
    assert failed[0].event_type == "message-failed"
