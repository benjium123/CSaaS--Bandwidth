"""Phase 21 smart-routing ranking unit tests.

These exercise the ranking algorithm directly with hand-built registries and API-created
org/numbers, then assert exact provider order and score relationships.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.db.base import set_org_context
from app.main import create_app
from app.models import Message, ProviderRate
from app.providers.domain import CarrierError
from app.providers.health import COOLDOWN_SECONDS, FAILURE_THRESHOLD, HealthRegistry
from app.providers.registry import CarrierRegistry
from app.routing import router as routing_svc
from app.services.smart_routing import (
    LIVEKIT_TRUNK_REASON,
    RouteCandidate,
    rank_routes,
    route_sentence,
)
from tests.conftest import FakeCarrier, auth_headers, make_org_with_number


class FakeClock:
    """Deterministic clock for circuit breakers."""

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
async def multi(engine, webhook_settings, clock):
    """An app wired with four fake carriers and an injected breaker clock."""
    application = create_app(webhook_settings)
    carriers = {
        "bandwidth": FakeCarrier(name="bandwidth"),
        "telnyx": FakeCarrier(name="telnyx"),
        "twilio": FakeCarrier(name="twilio"),
        "signalwire": FakeCarrier(name="signalwire"),
    }
    health = HealthRegistry(clock=clock)
    registry = CarrierRegistry(carriers, primary="bandwidth", health=health)
    application.state.carriers = registry
    application.state.carrier = carriers["bandwidth"]
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, registry


async def _org_with_numbers(client, email: str, numbers: list[tuple[str, str]]) -> tuple[str, dict]:
    token, org, _ = await make_org_with_number(client, email, "Org", numbers[0][0])
    h = auth_headers(token, org["id"])
    for e164, carrier in numbers[1:]:
        r = await client.post(
            "/api/v1/numbers", json={"e164": e164, "carrier": carrier}, headers=h
        )
        assert r.status_code == 201, r.text
    return token, org


async def test_rank_excludes_open_breaker_and_penalises_half_open(multi, session, clock):
    """Open is excluded outright. Half-open is ADMITTED while it still holds its probe
    token (that is how a carrier ever recovers - see smart_routing._health_penalty) and
    penalised 40 points only once the token has been spent by a concurrent send.
    Ranking itself must never spend the token."""
    client, registry = multi
    _token, org = await _org_with_numbers(
        client,
        "rank1@example.com",
        [
            ("+12145550100", "bandwidth"),
            ("+19725550300", "telnyx"),
            ("+14155550100", "twilio"),
        ],
    )
    org_id = uuid.UUID(org["id"])

    open_breaker = registry.health.breaker("bandwidth")
    for _ in range(FAILURE_THRESHOLD):
        open_breaker.record_failure(CarrierError("auth", "401", retryable=False))

    half_breaker = registry.health.breaker("telnyx")
    for _ in range(FAILURE_THRESHOLD):
        half_breaker.record_failure(CarrierError("auth", "401", retryable=False))

    clock.advance(COOLDOWN_SECONDS + 1)
    # Keep bandwidth open despite the same clock advancing telnyx into half-open.
    open_breaker.opened_at = clock.now + 1000
    assert half_breaker.state() == "half_open"

    set_org_context(session, org_id)
    candidates = await rank_routes(session, org_id, kind="sms", registry=registry)

    providers = {c.provider for c in candidates}
    assert "bandwidth" not in providers, "an OPEN breaker is excluded outright"
    assert "telnyx" in providers
    assert "twilio" in providers

    by_provider = {c.provider: c for c in candidates}
    unspent_score = by_provider["telnyx"].score
    assert half_breaker.allows_send() is True, "ranking must not consume the half-open probe"

    # allows_send() above just SPENT the token, exactly as the send path would. Rank again:
    # the same route must now carry the full 40-point health penalty.
    spent = await rank_routes(session, org_id, kind="sms", registry=registry)
    spent_by_provider = {c.provider: c for c in spent}
    assert spent_by_provider["telnyx"].score == unspent_score - 40.0
    assert spent_by_provider["telnyx"].score < spent_by_provider["twilio"].score


async def test_rank_lets_the_half_open_probe_through_so_the_breaker_can_recover(
    multi, session, clock
):
    """P14 DR-3 survives P21. THE REGRESSION THIS GUARDS: if a half-open breaker were
    penalised unconditionally, a healthy alternative would out-rank it forever, the walk
    would never reach it, its probe would never be spent, record_success would never fire,
    and the carrier would stay demoted for the life of the process."""
    client, registry = multi
    _token, org = await _org_with_numbers(
        client,
        "rank6@example.com",
        [
            ("+12145550150", "bandwidth"),
            ("+19725550350", "telnyx"),
        ],
    )
    org_id = uuid.UUID(org["id"])

    breaker = registry.health.breaker("bandwidth")
    for _ in range(FAILURE_THRESHOLD):
        breaker.record_failure(CarrierError("auth", "401", retryable=False))
    assert breaker.state() == "open"
    clock.advance(COOLDOWN_SECONDS + 1)
    assert breaker.state() == "half_open"

    set_org_context(session, org_id)
    candidates = await rank_routes(session, org_id, kind="sms", registry=registry)
    by_provider = {c.provider: c for c in candidates}

    # bandwidth and telnyx are priced identically for sms_out, so with no health penalty
    # the recovering carrier ties the healthy one and wins the alphabetical tie-break -
    # i.e. the walk really does reach it and really does spend the probe.
    assert by_provider["bandwidth"].score == by_provider["telnyx"].score
    assert candidates[0].provider == "bandwidth"
    assert by_provider["bandwidth"].health_state == "half_open"

    # The send path spends the token, the carrier answers, the breaker closes.
    assert breaker.allows_send() is True, "the one probe gets through"
    assert breaker.allows_send() is False, "but only one - no thundering herd"
    breaker.record_success()
    assert breaker.state() == "closed"
    assert breaker.consecutive_failures == 0

    recovered = await rank_routes(session, org_id, kind="sms", registry=registry)
    assert {c.provider: c.health_state for c in recovered}["bandwidth"] == "closed"


async def test_rank_excludes_breached_number_for_campaign_but_penalises_for_reply(multi, session):
    client, registry = multi
    bad = "+12145550110"
    clean = "+19725550310"
    token, org = await _org_with_numbers(
        client,
        "rank2@example.com",
        [(bad, "bandwidth"), (clean, "telnyx")],
    )
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)

    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725559999", "from": bad, "body": "spam"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    message_id = uuid.UUID(r.json()["id"])

    set_org_context(session, org_id)
    message = await session.get(Message, message_id)
    message.status = "rejected"
    message.error_code = "4750"
    message.carrier = "bandwidth"
    # Stamp the row an hour into the past. The reputation window is a trailing 7 days
    # ending at the real clock; a row created microseconds ago sits ON the boundary,
    # which is flaky. An hour is unambiguously inside the window on any machine.
    message.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()

    campaign = await rank_routes(
        session, org_id, kind="sms", registry=registry, is_campaign=True
    )
    campaign_e164s = {c.e164 for c in campaign}
    assert clean in campaign_e164s
    assert bad not in campaign_e164s

    reply = await rank_routes(
        session, org_id, kind="sms", registry=registry, is_campaign=False
    )
    by_e164 = {c.e164: c for c in reply}
    assert bad in by_e164
    assert clean in by_e164
    assert by_e164[bad].score < by_e164[clean].score


async def test_rank_prefers_cheaper_provider_all_else_equal(multi, session):
    client, registry = multi
    _token, org = await _org_with_numbers(
        client,
        "rank3@example.com",
        [
            ("+12145550120", "bandwidth"),
            ("+19725550320", "telnyx"),
        ],
    )
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    session.add(
        ProviderRate(
            id=uuid.uuid4(),
            org_id=org_id,
            provider="telnyx",
            metric="sms_out",
            unit_cost_micros=1000,
        )
    )
    await session.commit()

    candidates = await rank_routes(session, org_id, kind="sms", registry=registry)
    assert candidates[0].provider == "telnyx"


async def test_rank_pinned_provider_first_and_failover_only_when_allowed(multi, session):
    client, registry = multi
    _token, org = await _org_with_numbers(
        client,
        "rank4@example.com",
        [
            ("+12145550130", "bandwidth"),
            ("+19725550330", "telnyx"),
        ],
    )
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    policy = await routing_svc.get_policy(session, org_id)
    policy.pinned_carrier = "bandwidth"
    policy.allow_cross_carrier_failover = True
    policy.smart_routing = True
    await session.flush()

    candidates = await rank_routes(
        session, org_id, kind="sms", registry=registry, policy=policy
    )
    providers = [c.provider for c in candidates]
    assert providers[0] == "bandwidth"
    assert "telnyx" in providers
    assert providers.index("telnyx") > providers.index("bandwidth")

    policy.allow_cross_carrier_failover = False
    candidates = await rank_routes(
        session, org_id, kind="sms", registry=registry, policy=policy
    )
    assert {c.provider for c in candidates} == {"bandwidth"}


async def test_rank_is_deterministic_and_tie_breaks_by_name(multi, session):
    client, registry = multi
    _token, org = await _org_with_numbers(
        client,
        "rank5@example.com",
        [
            ("+12145550140", "bandwidth"),
            ("+19725550340", "telnyx"),
            ("+14155550140", "signalwire"),
        ],
    )
    org_id = uuid.UUID(org["id"])

    set_org_context(session, org_id)
    policy = await routing_svc.get_policy(session, org_id)
    policy.preference = []
    policy.allow_cross_carrier_failover = True
    await session.flush()

    first = await rank_routes(session, org_id, kind="sms", registry=registry, policy=policy)
    second = await rank_routes(session, org_id, kind="sms", registry=registry, policy=policy)

    assert [(c.provider, c.e164, c.score) for c in first] == [
        (c.provider, c.e164, c.score) for c in second
    ]
    assert [c.provider for c in first] == ["bandwidth", "signalwire", "telnyx"]


def test_route_sentence_shapes():
    def make_candidate(**overrides):
        defaults = {
            "provider": "telnyx",
            "number_id": None,
            "e164": "",
            "score": 0.0,
            "reasons": (),
            "health_state": "closed",
            "cost_micros": 0,
            "is_pinned": False,
        }
        defaults.update(overrides)
        return RouteCandidate(**defaults)

    assert (
        route_sentence(make_candidate(), kind="sms")
        == "Sent via Telnyx — cheapest healthy route"
    )
    assert (
        route_sentence(make_candidate(), kind="voice")
        == "Called via Telnyx — cheapest healthy route"
    )
    assert route_sentence(
        make_candidate(), kind="sms", failed_over_from="bandwidth"
    ) == "Failed over to Telnyx — Bandwidth unavailable"
    assert route_sentence(
        make_candidate(reasons=("preferred",)), kind="sms"
    ) == "Sent via Telnyx — your preferred provider"
    assert route_sentence(
        make_candidate(is_pinned=True), kind="sms"
    ) == "Sent via Telnyx — your chosen provider"
    assert route_sentence(
        make_candidate(), kind="sms", only_route=True
    ) == "Sent via Telnyx — your only route"
    assert route_sentence(
        make_candidate(health_state="half_open"), kind="sms"
    ) == "Sent via Telnyx — the healthiest route available"
    assert LIVEKIT_TRUNK_REASON == "Via your calling trunk"
