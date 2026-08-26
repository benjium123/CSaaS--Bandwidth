"""Phase 3b: which (number, carrier) pair sends a message, and what happens when it fails.

The assertions that matter most are the ones about *refusal*: an explicitly named carrier
must never be silently swapped for another, and a carrier switch must never happen inside a
conversation that already exists. Both are failure modes an operator only discovers from a
confused recipient.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.main import create_app
from app.providers.domain import CarrierError, SendResult
from app.providers.health import COOLDOWN_SECONDS, FAILURE_THRESHOLD, Breaker
from app.providers.registry import CarrierRegistry
from app.routing import router as routing_svc
from tests.conftest import FakeCarrier, auth_headers, make_org_with_number, register_and_login

BW = "+12145550100"
BW2 = "+12145550101"
TX = "+19725550300"
CONTACT = "+19725559999"


# ==================================================================================
# Fixtures: a deployment with two real carriers
# ==================================================================================
@pytest.fixture
async def multi(engine, webhook_settings):
    """An app with bandwidth + telnyx both configured and both fake."""
    application = create_app(webhook_settings)
    bandwidth = FakeCarrier(name="bandwidth")
    telnyx = FakeCarrier(name="telnyx")
    registry = CarrierRegistry(
        {"bandwidth": bandwidth, "telnyx": telnyx}, primary="bandwidth"
    )
    application.state.carriers = registry
    application.state.carrier = bandwidth
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, registry, bandwidth, telnyx


async def _org_with_numbers(client) -> tuple[str, dict]:
    """One org holding a bandwidth number, a second bandwidth number, and a telnyx one."""
    token, org, _ = await make_org_with_number(client, "r1@example.com", "Org R", BW)
    h = auth_headers(token, org["id"])
    for e164, carrier in ((BW2, "bandwidth"), (TX, "telnyx")):
        r = await client.post("/api/v1/numbers", json={"e164": e164, "carrier": carrier}, headers=h)
        assert r.status_code == 201, r.text
    return token, org


async def _set_policy(client, h, **fields) -> dict:
    r = await client.patch("/api/v1/routing/policy", json=fields, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


# ==================================================================================
# Precedence
# ==================================================================================
async def test_explicit_from_pins_its_owning_carrier(multi):
    client, _, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "from": TX, "body": "hi"}, headers=h
    )
    assert r.status_code == 201, r.text
    assert r.json()["from_e164"] == TX
    assert len(telnyx.sent) == 1, "the telnyx-hosted number must send via telnyx"
    assert bandwidth.sent == []


async def test_explicit_carrier_picks_a_number_on_that_carrier(multi):
    client, _, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "hi", "carrier": "telnyx"}, headers=h
    )
    assert r.status_code == 201, r.text
    assert r.json()["from_e164"] == TX
    assert len(telnyx.sent) == 1
    assert bandwidth.sent == []


async def test_default_send_uses_the_primary_carrier(multi):
    client, _, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])

    r = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)
    assert r.status_code == 201, r.text
    assert len(bandwidth.sent) == 1
    assert telnyx.sent == []


async def test_preference_order_moves_traffic(multi):
    client, _, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])

    await _set_policy(client, h, preference=["telnyx", "bandwidth"])
    r = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)
    assert r.status_code == 201, r.text
    assert len(telnyx.sent) == 1, "preference must actually change the carrier"


async def test_pinned_carrier_overrides_preference(multi):
    client, _, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])

    await _set_policy(client, h, preference=["bandwidth", "telnyx"], pinned_carrier="telnyx")
    r = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)
    assert r.status_code == 201
    assert len(telnyx.sent) == 1


# ==================================================================================
# Refusal, not substitution
# ==================================================================================
async def test_explicit_carrier_that_is_unhealthy_errors(multi):
    """THE LOAD-BEARING ONE. A named carrier is honoured or refused, never swapped.

    Silent substitution is how an operator finds out at 2am that half their traffic left
    on the wrong brand.
    """
    client, registry, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])

    breaker = registry.health.breaker("telnyx")
    for _ in range(FAILURE_THRESHOLD):
        breaker.record_failure(CarrierError("carrier_transient", None, retryable=True))
    assert breaker.state() == "open"

    r = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "hi", "carrier": "telnyx"}, headers=h
    )
    assert r.status_code == 503, r.text
    assert bandwidth.sent == [], "must NOT quietly fall back to the other carrier"
    assert telnyx.sent == []


async def test_unknown_carrier_is_refused(multi):
    client, _, _, _ = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "hi", "carrier": "sinch"}, headers=h
    )
    assert r.status_code == 503


async def test_policy_rejects_an_unconfigured_carrier(multi):
    """A policy that silently does nothing is worse than one that refuses to save."""
    client, _, _, _ = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])

    r = await client.patch(
        "/api/v1/routing/policy", json={"preference": ["telnyx", "sinch"]}, headers=h
    )
    assert r.status_code == 422
    assert "sinch" in r.text


# ==================================================================================
# Failover
# ==================================================================================
async def test_intra_carrier_failover_tries_the_other_number(multi):
    client, _, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])

    bandwidth.scripted = [
        SendResult("rejected", None, CarrierError("carrier_transient", "5001", retryable=True)),
        SendResult("accepted", "prov-2", None),
    ]
    r = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "accepted"
    assert len(bandwidth.sent) == 2, "must retry on the second bandwidth number"
    assert telnyx.sent == [], "cross-carrier is off by default"


async def test_a_permanent_rejection_is_not_retried_elsewhere(multi):
    """An invalid request fails identically everywhere. Retrying it just spreads the bug."""
    client, _, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])

    bandwidth.scripted = [
        SendResult("rejected", None, CarrierError("invalid_request", "4302", retryable=False)),
    ]
    r = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)
    assert r.status_code == 201
    assert r.json()["status"] == "rejected"
    assert len(bandwidth.sent) == 1, "no retry"
    assert telnyx.sent == []


async def test_cross_carrier_failover_when_opted_in_for_new_outreach(multi):
    client, _, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])
    await _set_policy(
        client, h, allow_cross_carrier_failover=True, allow_intra_carrier_failover=False
    )

    bandwidth.scripted = [
        SendResult("rejected", None, CarrierError("carrier_unreachable", None, retryable=True)),
    ]
    r = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "accepted"
    assert len(telnyx.sent) == 1, "should have crossed to telnyx"
    assert r.json()["from_e164"] == TX, "and the message records the number that sent it"


async def test_cross_carrier_failover_is_refused_mid_conversation(multi):
    """A thread that has been spoken to keeps its sender or it does not get sent.

    Threads are keyed by our_e164, so a carrier switch does not continue the conversation -
    it starts a second one beside it, from a number the recipient has never seen.
    """
    client, _, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])
    await _set_policy(
        client, h, allow_cross_carrier_failover=True, allow_intra_carrier_failover=False
    )

    first = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "one"}, headers=h)
    assert first.status_code == 201
    assert len(bandwidth.sent) == 1

    bandwidth.scripted = [
        SendResult("rejected", None, CarrierError("carrier_unreachable", None, retryable=True)),
    ]
    second = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "two"}, headers=h)
    assert second.status_code == 201
    assert second.json()["status"] == "rejected", "silence beats a stranger replying"
    assert telnyx.sent == [], "must NOT cross carriers inside an existing conversation"


async def test_the_carrier_that_sent_is_recorded(multi, session):
    import sqlalchemy as sa

    from app.db.base import ALLOW_UNSCOPED_KEY
    from app.models import Message

    client, _, _, telnyx = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "hi", "carrier": "telnyx"}, headers=h
    )
    row = (
        await session.execute(
            sa.select(Message)
            .where(Message.id == uuid.UUID(r.json()["id"]))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    assert row.carrier == "telnyx"
    assert row.from_e164 == TX


# ==================================================================================
# Circuit breaker
# ==================================================================================
def test_breaker_opens_only_on_carrier_faults():
    b = Breaker("x")
    for _ in range(FAILURE_THRESHOLD * 2):
        b.record_failure(CarrierError("invalid_request", "4302", retryable=False))
    assert b.state() == "closed", "our own bad requests are not their outage"

    for _ in range(FAILURE_THRESHOLD * 2):
        b.record_failure(CarrierError("auth", None, retryable=False))
    assert b.state() == "closed", "bad credentials are not an outage either"

    for _ in range(FAILURE_THRESHOLD):
        b.record_failure(CarrierError("carrier_transient", None, retryable=True))
    assert b.state() == "open"


def test_breaker_half_opens_then_closes_on_success():
    b = Breaker("x")
    for _ in range(FAILURE_THRESHOLD):
        b.record_failure(CarrierError("carrier_unreachable", None, retryable=True), now=1000.0)
    assert b.state(now=1000.0) == "open"
    assert not b.allows_send(now=1000.0)

    later = 1000.0 + COOLDOWN_SECONDS + 1
    assert b.state(now=later) == "half_open"
    assert b.allows_send(now=later), "one probe gets through"
    assert not b.allows_send(now=later), "but only one - no thundering herd"

    b.record_success()
    assert b.state() == "closed"


def test_breaker_success_resets_the_streak():
    b = Breaker("x")
    for _ in range(FAILURE_THRESHOLD - 1):
        b.record_failure(CarrierError("rate_limited", None, retryable=True))
    b.record_success()
    b.record_failure(CarrierError("rate_limited", None, retryable=True))
    assert b.state() == "closed", "a success must clear the streak, not just pause it"


# ==================================================================================
# Per-carrier webhook verification
# ==================================================================================
def test_telnyx_signature_roundtrip_and_tamper():
    from app.providers.telnyx import webhooks as tx

    key = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        key.public_key().public_bytes_raw()
    ).decode()
    body = b'{"data":{"event_type":"message.received"}}'
    ts = str(int(time.time()))
    sig = base64.b64encode(key.sign(f"{ts}|".encode() + body)).decode()
    headers = {"telnyx-signature-ed25519": sig, "telnyx-timestamp": ts}

    assert tx.verify(headers, public_b64, body)
    assert not tx.verify(headers, public_b64, body + b"x"), "tampered body must fail"
    assert not tx.verify({}, public_b64, body)
    assert not tx.verify(headers, "", body), "no key configured = no trust"


def test_telnyx_signature_rejects_a_replay():
    """A valid signature with no freshness window is a replay primitive."""
    from app.providers.telnyx import webhooks as tx

    key = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    body = b"{}"
    stale = str(int(time.time()) - tx.TOLERANCE_SECONDS - 60)
    sig = base64.b64encode(key.sign(f"{stale}|".encode() + body)).decode()

    assert not tx.verify(
        {"telnyx-signature-ed25519": sig, "telnyx-timestamp": stale}, public_b64, body
    )


def test_signalwire_signature_roundtrip_and_tamper():
    from app.providers.signalwire import webhooks as sw

    token = "sekrit"
    url = "https://api.example.com/api/v1/webhooks/signalwire/messaging"
    body = b"MessageSid=SM1&From=%2B12145550100&To=%2B19725559999&Body=hi&NumMedia=0"
    expected = sw.expected_signature(url, dict(sw._form(body)), token)

    assert sw.verify({"X-SignalWire-Signature": expected}, token, url, body)
    assert sw.verify({"X-Twilio-Signature": expected}, token, url, body), "accept both names"
    assert not sw.verify({"X-Twilio-Signature": expected}, token, url, body + b"&x=1")
    assert not sw.verify({"X-Twilio-Signature": expected}, token, url + "/other", body), (
        "the signature covers the URL, so a different URL must not verify"
    )
    assert not sw.verify({"X-Twilio-Signature": "nope"}, token, url, body)


def test_signalwire_signature_matches_the_twilio_algorithm():
    """Pinned against the published algorithm, not against our own implementation."""
    from app.providers.signalwire import webhooks as sw

    token = "12345"
    url = "https://example.com/hook"
    params = {"B": "2", "A": "1"}
    manual = base64.b64encode(
        hmac.new(token.encode(), (url + "A1B2").encode(), hashlib.sha1).digest()
    ).decode()
    assert sw.expected_signature(url, params, token) == manual


def test_signalwire_parses_inbound_and_status():
    from app.providers.domain import DeliveryReceipt, InboundMessage
    from app.providers.signalwire import webhooks as sw

    inbound = sw.parse(
        b"MessageSid=SM1&From=%2B19725559999&To=%2B12145550100&Body=hello&NumMedia=1"
        b"&MediaUrl0=https%3A%2F%2Fx.signalwire.com%2Fa.png"
    )[0]
    assert isinstance(inbound, InboundMessage)
    assert inbound.from_ == "+19725559999"
    assert inbound.our_number == "+12145550100"
    assert inbound.media == ("https://x.signalwire.com/a.png",)

    dlr = sw.parse(b"MessageSid=SM1&MessageStatus=delivered")[0]
    assert isinstance(dlr, DeliveryReceipt)
    assert dlr.event_type == "message-delivered"


def test_unmapped_statuses_are_never_guessed_into_a_terminal_state():
    from app.providers.domain import UnknownEvent
    from app.providers.signalwire import webhooks as sw
    from app.providers.telnyx import webhooks as tx

    assert isinstance(sw.parse(b"MessageSid=SM1&MessageStatus=teleported")[0], UnknownEvent)

    payload = json.dumps(
        {
            "data": {
                "event_type": "message.finalized",
                "payload": {"id": "m1", "to": [{"phone_number": "+1", "status": "wat"}]},
            }
        }
    ).encode()
    assert isinstance(tx.parse(payload)[0], UnknownEvent)


def test_telnyx_parses_inbound_media():
    from app.providers.domain import InboundMessage
    from app.providers.telnyx import webhooks as tx

    payload = json.dumps(
        {
            "data": {
                "event_type": "message.received",
                "occurred_at": "2026-08-26T10:00:00Z",
                "payload": {
                    "id": "m9",
                    "from": {"phone_number": "+19725559999"},
                    "to": [{"phone_number": "+19725550300"}],
                    "text": "pic",
                    "media": [{"url": "https://media.telnyx.com/a.png"}],
                    "parts": 1,
                },
            }
        }
    ).encode()
    event = tx.parse(payload)[0]
    assert isinstance(event, InboundMessage)
    assert event.our_number == "+19725550300"
    assert event.media == ("https://media.telnyx.com/a.png",)


def test_telnyx_never_attaches_credentials_to_a_media_fetch():
    from app.providers.telnyx.adapter import TelnyxMessagingCarrier

    carrier = TelnyxMessagingCarrier(api_key="k")
    assert carrier.media_auth("https://media.telnyx.com/a.png") is None


def test_signalwire_media_auth_is_host_checked():
    from app.providers.signalwire.adapter import SignalWireMessagingCarrier

    carrier = SignalWireMessagingCarrier(
        project_id="p", api_token="t", space_url="x.signalwire.com"
    )
    assert carrier.media_auth("https://x.signalwire.com/a.png") == ("p", "t")
    assert carrier.media_auth("https://evil.example.com/a.png") is None


# ==================================================================================
# Registry / status surface
# ==================================================================================
async def test_carrier_status_never_leaks_a_credential(multi):
    client, _, _, _ = multi
    token, org = await _org_with_numbers(client)
    r = await client.get("/api/v1/routing/carriers", headers=auth_headers(token, org["id"]))
    assert r.status_code == 200
    names = {c["name"] for c in r.json()}
    assert names == {"bandwidth", "telnyx"}
    blob = json.dumps(r.json()).lower()
    for forbidden in ("secret", "token", "password", "api_key", "apikey"):
        assert forbidden not in blob


async def test_an_org_that_never_touched_routing_behaves_as_before(multi):
    """The defaults must reproduce pre-phase-3b behaviour exactly."""
    client, _, _, _ = multi
    token, org = await _org_with_numbers(client)
    r = await client.get("/api/v1/routing/policy", headers=auth_headers(token, org["id"]))
    body = r.json()
    assert body["allow_cross_carrier_failover"] is False, "must be opt-in, never a default"
    assert body["allow_intra_carrier_failover"] is True
    assert body["preference"] == []
    assert body["pinned_carrier"] is None


async def test_routing_policy_is_org_scoped(multi):
    client, _, _, _ = multi
    token_a, org_a = await _org_with_numbers(client)
    token_b = await register_and_login(client, "r2@example.com")
    from tests.conftest import create_org

    org_b = await create_org(client, token_b, "Org S")

    await _set_policy(client, auth_headers(token_a, org_a["id"]), pinned_carrier="telnyx")
    other = await client.get(
        "/api/v1/routing/policy", headers=auth_headers(token_b, org_b["id"])
    )
    assert other.json()["pinned_carrier"] is None
