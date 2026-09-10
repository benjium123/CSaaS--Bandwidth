"""Phase 21 / D28: voice call failover walks the same ranked plan the SMS sender walks.

The gate test in this file is deliberately explicit: a primary voice carrier that rejects
with a breaker-opening ``auth`` error must NOT end the call. The service must move to the
next provider in the ranked plan, exactly once each, and the call row must reflect the
attempt. The test is written so that reducing ``create_outbound_call`` to a single attempt
fails on the concrete ``telnyx.create_calls`` count.
"""

from __future__ import annotations

import inspect

import httpx
import pytest

from app.main import create_app
from app.providers.domain import CarrierError
from app.providers.health import HealthRegistry
from app.providers.registry import CarrierRegistry
from app.providers.voice import CreateCallResult
from app.services import calls as calls_svc
from app.services import smart_routing
from tests.conftest import auth_headers, make_org_with_number
from tests.test_voice_webhooks import FakeVoiceCarrier

PRIMARY_NUM = "+12145550100"
FALLBACK_NUM = "+19725550300"
CONTACT = "+19725559999"


class FakeClock:
    """Deterministic, manually advanced - lets the gate test drive breaker cooldown and
    half-open recovery without sleeping or monkeypatching real time (P14 DR-3)."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def two_voice_carriers(engine, webhook_settings, clock):
    """bandwidth (primary) + telnyx (fallback), both FakeVoiceCarrier, sharing one clock."""
    application = create_app(webhook_settings)
    bandwidth = FakeVoiceCarrier(name="bandwidth")
    telnyx = FakeVoiceCarrier(name="telnyx")
    health = HealthRegistry(clock=clock)
    registry = CarrierRegistry(
        {"bandwidth": bandwidth, "telnyx": telnyx}, primary="bandwidth", health=health
    )
    application.state.carriers = registry
    application.state.carrier = bandwidth
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, registry, bandwidth, telnyx


async def _org_with_two_numbers(client: httpx.AsyncClient, email: str) -> tuple[str, dict]:
    token, org, _ = await make_org_with_number(client, email, "Org P21 Voice", PRIMARY_NUM)
    h = auth_headers(token, org["id"])
    r = await client.post(
        "/api/v1/numbers", json={"e164": FALLBACK_NUM, "carrier": "telnyx"}, headers=h
    )
    assert r.status_code == 201, r.text
    return token, org


async def test_voice_create_call_walks_plan_on_breaker_error(two_voice_carriers):
    """THE D28 GATE.

    Bandwidth (primary) rejects with an auth error - a dead credential, not a bad request -
    so the call must walk to the next healthy voice provider. The explicit ``from`` is the
    org's bandwidth number, and the dial plan must reach telnyx before the endpoint returns.
    """
    client, _, bandwidth, telnyx = two_voice_carriers
    token, org = await _org_with_two_numbers(client, "p21-failover@example.com")
    h = auth_headers(token, org["id"])

    bandwidth.scripted_results = [
        CreateCallResult(
            "rejected",
            None,
            "credentials rejected (401)",
            CarrierError("auth", "401", retryable=False),
        )
    ]

    r = await client.post(
        "/api/v1/calls", json={"to": CONTACT, "from": PRIMARY_NUM}, headers=h
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] != "failed"

    # THIS TEST MUST FAIL if the loop in create_outbound_call is reduced to a single attempt.
    assert len(bandwidth.create_calls) == 1
    assert len(telnyx.create_calls) == 1

    # route_reason asserted in test_p21_voice_taxonomy


async def test_voice_non_breaker_error_stops_the_walk(two_voice_carriers):
    """An invalid_request is OUR bug, not a carrier fault. Trying another provider would
    spread the same bug and must not happen. The breaker must remain closed."""
    client, registry, bandwidth, telnyx = two_voice_carriers
    token, org = await _org_with_two_numbers(client, "p21-invalid@example.com")
    h = auth_headers(token, org["id"])

    bandwidth.scripted_results = [
        CreateCallResult(
            "rejected",
            None,
            "invalid request (40001)",
            CarrierError("invalid_request", "40001", retryable=False),
        )
    ]

    r = await client.post(
        "/api/v1/calls", json={"to": CONTACT, "from": PRIMARY_NUM}, headers=h
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "failed"

    assert len(bandwidth.create_calls) == 1
    assert telnyx.create_calls == []
    assert registry.health.breaker("bandwidth").state() == "closed"


async def _set_policy(client, h, **fields) -> dict:
    r = await client.patch("/api/v1/routing/policy", json=fields, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def test_voice_failover_refused_when_cross_carrier_failover_is_off(two_voice_carriers):
    """P21: voice honours the org's failover switch EXACTLY like the SMS walk does.

    An org that turned cross-carrier failover off meant it for calls too - dialling from
    another provider's number shows the recipient a different caller id, which is the
    surprise the switch exists to prevent. Same fixture and same dead credential as the
    D28 gate; only the policy differs, so this test isolates the policy and nothing else.
    """
    client, registry, bandwidth, telnyx = two_voice_carriers
    token, org = await _org_with_two_numbers(client, "p21-policy-off@example.com")
    h = auth_headers(token, org["id"])
    await _set_policy(client, h, allow_cross_carrier_failover=False)

    bandwidth.scripted_results = [
        CreateCallResult(
            "rejected",
            None,
            "credentials rejected (401)",
            CarrierError("auth", "401", retryable=False),
        )
    ]

    r = await client.post(
        "/api/v1/calls", json={"to": CONTACT, "from": PRIMARY_NUM}, headers=h
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "failed", "silence beats a stranger's caller id"
    assert len(bandwidth.create_calls) == 1
    assert telnyx.create_calls == [], "must NOT cross providers when the org said not to"


async def test_voice_failover_allowed_when_cross_carrier_failover_is_on(two_voice_carriers):
    """The same call, the same dead credential, the switch ON - the walk happens.

    Paired deliberately with the test above: together they prove the ONLY thing deciding
    whether voice fails over is the org's policy, not the error and not the fixture.
    """
    client, registry, bandwidth, telnyx = two_voice_carriers
    token, org = await _org_with_two_numbers(client, "p21-policy-on@example.com")
    h = auth_headers(token, org["id"])
    await _set_policy(client, h, allow_cross_carrier_failover=True)

    bandwidth.scripted_results = [
        CreateCallResult(
            "rejected",
            None,
            "credentials rejected (401)",
            CarrierError("auth", "401", retryable=False),
        )
    ]

    r = await client.post(
        "/api/v1/calls", json={"to": CONTACT, "from": PRIMARY_NUM}, headers=h
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] != "failed"
    assert len(bandwidth.create_calls) == 1
    assert len(telnyx.create_calls) == 1
    assert "Failed over to Telnyx" in (body["route_reason"] or "")


async def test_voice_pinned_carrier_is_not_left_for_a_fallback(two_voice_carriers):
    """A pin is stricter than the failover switch: with cross-carrier failover off, a
    pinned provider's failure ends the call rather than quietly leaving the pin."""
    client, registry, bandwidth, telnyx = two_voice_carriers
    token, org = await _org_with_two_numbers(client, "p21-pinned@example.com")
    h = auth_headers(token, org["id"])
    await _set_policy(
        client, h, pinned_carrier="bandwidth", allow_cross_carrier_failover=False
    )

    bandwidth.scripted_results = [
        CreateCallResult(
            "rejected",
            None,
            "credentials rejected (401)",
            CarrierError("auth", "401", retryable=False),
        )
    ]

    r = await client.post(
        "/api/v1/calls", json={"to": CONTACT, "from": PRIMARY_NUM}, headers=h
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "failed"
    assert telnyx.create_calls == [], "a pin is not abandoned on the first failure"


async def test_voice_single_provider_is_unchanged(engine, webhook_settings):
    """The no-routes path must still attempt exactly one carrier and return normally."""
    application = create_app(webhook_settings)
    bandwidth = FakeVoiceCarrier(name="bandwidth")
    registry = CarrierRegistry({"bandwidth": bandwidth}, primary="bandwidth")
    application.state.carriers = registry
    application.state.carrier = bandwidth
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token, org, _ = await make_org_with_number(
            client, "p21-single@example.com", "Org P21 Single", PRIMARY_NUM
        )
        h = auth_headers(token, org["id"])

        r = await client.post(
            "/api/v1/calls", json={"to": CONTACT, "from": PRIMARY_NUM}, headers=h
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] != "failed"
        assert len(bandwidth.create_calls) == 1


def test_livekit_trunk_reason_constant_is_plain_words():
    """The LiveKit path records the trunk reason and does NOT go through the provider-API
    failover walk. We assert the constant directly and that the provider-API service has no
    LiveKit routing parameter - it is not the path that handles a via="room" call."""
    assert smart_routing.LIVEKIT_TRUNK_REASON == "Via your calling trunk"

    # services/calls.create_outbound_call is the provider-API path; the LiveKit path has its
    # own code in app.voice_plane, so this function must not grow a `via` parameter.
    assert "via" not in inspect.signature(calls_svc.create_outbound_call).parameters
