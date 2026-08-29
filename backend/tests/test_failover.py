"""Phase 14: failover actually EXECUTES end-to-end, including the credentials-death case
the gate names (DR-1/DR-2/DR-3).

THE GATE (local form, per docs/plans/phase-14-plan.md's test spec): two fake carriers, a
primary scripted to fail with `auth` errors (a revoked/rotated credential - operationally a
dead carrier, not a bad request) mid-run, org policy opted into cross-carrier failover.
Every message must land `sent` exactly once on SOME carrier - never lost, never doubled -
and after the breaker's cooldown, the next send returns to the primary.

The voice section is deliberately NOT a parallel gate: `services/calls.create_outbound_call`
does not walk a routing plan at all today (see its test below, and the final report). That
test pins the CURRENT behaviour rather than asserting failover this phase never implemented.
"""

from __future__ import annotations

import httpx
import pytest

from app.main import create_app
from app.providers.domain import CarrierError, SendResult
from app.providers.health import COOLDOWN_SECONDS, FAILURE_THRESHOLD, Breaker, HealthRegistry
from app.providers.registry import CarrierRegistry
from app.providers.voice import CreateCallResult
from tests.conftest import FakeCarrier, auth_headers, make_org_with_number
from tests.test_voice_webhooks import FakeVoiceCarrier

PRIMARY_NUM = "+12145550100"
FALLBACK_NUM = "+19725550300"
CONTACT = "+19725559999"


class FakeClock:
    """Deterministic, manually advanced - lets the GATE test drive breaker cooldown and
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
async def multi(engine, webhook_settings, clock):
    """bandwidth (primary) + telnyx (fallback), both fake, sharing ONE injected clock so
    breaker cooldown/recovery can be driven deterministically."""
    application = create_app(webhook_settings)
    bandwidth = FakeCarrier(name="bandwidth")
    telnyx = FakeCarrier(name="telnyx")
    health = HealthRegistry(clock=clock)
    registry = CarrierRegistry(
        {"bandwidth": bandwidth, "telnyx": telnyx}, primary="bandwidth", health=health
    )
    application.state.carriers = registry
    application.state.carrier = bandwidth
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, registry, bandwidth, telnyx


async def _org_with_numbers(client, email: str = "fo1@example.com") -> tuple[str, dict]:
    token, org, _ = await make_org_with_number(client, email, "Org F", PRIMARY_NUM)
    h = auth_headers(token, org["id"])
    r = await client.post(
        "/api/v1/numbers", json={"e164": FALLBACK_NUM, "carrier": "telnyx"}, headers=h
    )
    assert r.status_code == 201, r.text
    return token, org


async def _set_policy(client, h, **fields) -> dict:
    r = await client.patch("/api/v1/routing/policy", json=fields, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


# ==================================================================================
# Breaker: auth failures open it (NEW), invalid_request never does, injected-clock
# cooldown -> half-open -> closed.
# ==================================================================================
def test_breaker_opens_on_five_consecutive_auth_failures():
    """P14 DR-1: NEW behaviour. A revoked/rotated credential is operationally a dead
    carrier, so `auth` now joins the breaker-opening categories."""
    b = Breaker("x")
    for _ in range(FAILURE_THRESHOLD):
        b.record_failure(CarrierError("auth", "401", retryable=False))
    assert b.state() == "open"


def test_breaker_never_opens_on_invalid_request():
    """`invalid_request` stays excluded - still our bug, not the carrier's."""
    b = Breaker("x")
    for _ in range(FAILURE_THRESHOLD * 3):
        b.record_failure(CarrierError("invalid_request", "4302", retryable=False))
    assert b.state() == "closed"


def test_breaker_half_opens_after_injected_clock_cooldown_then_closes_on_success():
    fake_clock = FakeClock(start=0.0)
    b = Breaker("x", clock=fake_clock)
    for _ in range(FAILURE_THRESHOLD):
        b.record_failure(CarrierError("auth", None, retryable=False))
    assert b.state() == "open"
    assert not b.allows_send()

    fake_clock.advance(COOLDOWN_SECONDS + 1)
    assert b.state() == "half_open"
    assert b.allows_send(), "one probe gets through"
    assert not b.allows_send(), "but only one - no thundering herd"

    b.record_success()
    assert b.state() == "closed"


# ==================================================================================
# THE GATE
# ==================================================================================
async def test_gate_auth_failure_fails_over_and_recovers_after_cooldown(multi, clock):
    """A dead credential (auth error) on the primary carrier fails traffic over to a
    healthy fallback, IN THE SAME REQUEST, with every message landing `sent` exactly once
    on SOME carrier - never lost, never doubled. After cooldown, the next send returns to
    the primary (the breaker's existing half-open probe, DR-3 - no new machinery).
    """
    client, registry, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client)
    h = auth_headers(token, org["id"])
    await _set_policy(client, h, allow_cross_carrier_failover=True)

    # Bandwidth (primary) scripted to fail with `auth` on every attempt - simulating a
    # revoked/rotated credential, the gate's named scenario. Five sends = exactly
    # FAILURE_THRESHOLD consecutive auth failures, which must open the breaker.
    bandwidth.scripted = [
        SendResult("rejected", None, CarrierError("auth", "401", retryable=False))
        for _ in range(FAILURE_THRESHOLD)
    ]

    # Each send is a DIFFERENT contact - reusing one would make sends 2-5 replies inside
    # an existing thread, and D12 refuses cross-carrier failover for those (tested
    # separately below). This loop exercises the "new outreach" branch on purpose.
    sent_ids = set()
    for i in range(FAILURE_THRESHOLD):
        contact = f"+1972555{9990 + i}"
        r = await client.post(
            "/api/v1/messages",
            json={"to": contact, "body": f"msg {i}"},
            headers=h,
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["status"] == "accepted", body
        assert body["from_e164"] == FALLBACK_NUM, "failed over to the healthy carrier"
        assert body["id"] not in sent_ids, "no message id was doubled"
        sent_ids.add(body["id"])

    assert len(sent_ids) == FAILURE_THRESHOLD, "every message landed exactly once"
    assert len(bandwidth.sent) == FAILURE_THRESHOLD, "primary was tried once per message"
    assert len(telnyx.sent) == FAILURE_THRESHOLD, "fallback carried every failed-over message"

    breaker = registry.health.breaker("bandwidth")
    assert breaker.state() == "open", "5 consecutive auth failures must open the breaker"

    # Recovery: after cooldown, the very next send goes back to the primary.
    clock.advance(COOLDOWN_SECONDS + 1)
    bandwidth.scripted = [SendResult("accepted", "prov-recovered", None)]
    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725559100", "body": "recovered", "allow_reassign": True},
        headers=h,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["from_e164"] == PRIMARY_NUM, "recovery: traffic returns to the primary"
    assert body["status"] == "accepted"
    assert breaker.state() == "closed", "success on the half-open probe closes the breaker"


async def test_mid_thread_reply_refuses_cross_carrier_even_on_auth_failure(multi):
    """D12 holds under the NEW auth-failover path too: a thread that has been spoken to
    keeps its sender or does not send - it never crosses carriers mid-conversation, even
    when the primary's credential is dead and cross-carrier failover is opted in."""
    client, _, bandwidth, telnyx = multi
    token, org = await _org_with_numbers(client, email="fo2@example.com")
    h = auth_headers(token, org["id"])
    await _set_policy(client, h, allow_cross_carrier_failover=True)

    first = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "one"}, headers=h)
    assert first.status_code == 201
    assert first.json()["status"] == "accepted"
    assert len(bandwidth.sent) == 1

    bandwidth.scripted = [
        SendResult("rejected", None, CarrierError("auth", "401", retryable=False))
    ]
    second = await client.post("/api/v1/messages", json={"to": CONTACT, "body": "two"}, headers=h)
    assert second.status_code == 201
    assert second.json()["status"] == "rejected", "silence beats a stranger replying"
    assert telnyx.sent == [], "must NOT cross carriers inside an existing conversation"


# ==================================================================================
# Voice: services/calls.create_outbound_call does NOT walk a routing plan today.
# ==================================================================================
async def test_voice_create_call_does_not_fail_over_today(engine, webhook_settings):
    """ADJUDICATED as a DR-2 amendment (Fable), not merely deferred: voice does NOT get a
    failover walk in P14. `services/calls.create_outbound_call` resolves exactly ONE
    (carrier, from) via `_resolve_outbound` and calls `create_call` ONCE - no breaker
    check, no cross-carrier retry - and today a dead credential on a voice call ends that
    call as `failed` even with a second, healthy, voice-capable carrier configured and an
    active number on it. The reason for the ruling: unlike `SendResult`/`CarrierError` on
    the messaging side, `CreateCallResult` carries no error TAXONOMY (no `category`, no
    `retryable`) - there is nothing for a voice dispatch_with_failover-equivalent to
    branch on to tell "dead credential, try elsewhere" apart from "bad number, do not
    retry," and there is no `RoutePlan` on the dial path to walk in the first place. Both
    would need to be designed and built, not bolted on inside this phase. This test PINS
    the current single-attempt behaviour as the recorded baseline; see OPEN_ISSUES for the
    tracked follow-up phase.
    """
    application = create_app(webhook_settings)
    bandwidth = FakeVoiceCarrier(name="bandwidth")
    telnyx = FakeVoiceCarrier(name="telnyx")
    bandwidth.scripted_results = [CreateCallResult("rejected", None, "credentials rejected (401)")]
    registry = CarrierRegistry({"bandwidth": bandwidth, "telnyx": telnyx}, primary="bandwidth")
    application.state.carriers = registry
    application.state.carrier = bandwidth

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token, org, _ = await make_org_with_number(
            client, "voice1@example.com", "Org V", PRIMARY_NUM
        )
        h = auth_headers(token, org["id"])
        r = await client.post(
            "/api/v1/numbers", json={"e164": FALLBACK_NUM, "carrier": "telnyx"}, headers=h
        )
        assert r.status_code == 201, r.text

        r = await client.post(
            "/api/v1/calls", json={"to": "+19725550199", "from": PRIMARY_NUM}, headers=h
        )
        assert r.status_code == 201, r.text
        body = r.json()

    assert body["status"] == "failed", "current behaviour: no voice failover walk"
    assert len(bandwidth.create_calls) == 1, "the primary was tried once"
    assert telnyx.create_calls == [], (
        "GAP: a second, healthy voice-capable carrier with an active number was never "
        "attempted - see the final report for what a future phase would need to add"
    )
