"""P21 backend VERIFICATION probes (Opus verifier, 2026-09-10).

Independent of the drafter's own tests: every probe here goes through a REAL entry point
(rank_routes, plan_route, POST /api/v1/messages, POST /api/v1/calls, outbound_tick,
seed_org_defaults, the adapters themselves) and asserts a concrete value.

Nothing in this file is allowed to fix app code - it only measures it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from random import Random

import httpx
import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.main import create_app
from app.models import (
    Contact,
    ContactList,
    ContactListRow,
    Message,
    ProviderAccount,
    ProviderRate,
)
from app.models.routing import RoutingPolicy
from app.models.voice import Call
from app.providers.domain import CarrierError
from app.providers.health import HealthRegistry, opens_breaker
from app.providers.registry import CarrierRegistry
from app.providers.voice import CreateCallResult
from app.routing import router as routing_svc
from app.services import defaults as defaults_svc
from app.services import outbound as outbound_svc
from app.services import smart_routing
from tests.conftest import (
    FakeCarrier,
    auth_headers,
    create_org,
    make_org_with_number,
    register_and_login,
)
from tests.test_voice_webhooks import FakeVoiceCarrier

CONTACT = "+19725559999"


# ----------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------
class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
async def sms_app(engine, webhook_settings):
    """Four fake messaging carriers under one registry + injected breaker clock."""
    application = create_app(webhook_settings)
    carriers = {
        "bandwidth": FakeCarrier(name="bandwidth"),
        "telnyx": FakeCarrier(name="telnyx"),
        "twilio": FakeCarrier(name="twilio"),
        "signalwire": FakeCarrier(name="signalwire"),
    }
    registry = CarrierRegistry(
        carriers, primary="bandwidth", health=HealthRegistry(clock=FakeClock())
    )
    application.state.carriers = registry
    application.state.carrier = carriers["bandwidth"]
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, registry, carriers


@pytest.fixture
async def voice_app(engine, webhook_settings):
    application = create_app(webhook_settings)
    bandwidth = FakeVoiceCarrier(name="bandwidth")
    telnyx = FakeVoiceCarrier(name="telnyx")
    registry = CarrierRegistry(
        {"bandwidth": bandwidth, "telnyx": telnyx},
        primary="bandwidth",
        health=HealthRegistry(clock=FakeClock()),
    )
    application.state.carriers = registry
    application.state.carrier = bandwidth
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, registry, bandwidth, telnyx


async def _org(client, email: str, numbers: list[tuple[str, str]]) -> tuple[str, dict]:
    token = await register_and_login(client, email)
    org = await create_org(client, token, f"Org {email}")
    h = auth_headers(token, org["id"])
    for e164, carrier in numbers:
        r = await client.post("/api/v1/numbers", json={"e164": e164, "carrier": carrier}, headers=h)
        assert r.status_code == 201, r.text
    return token, org


# ==================================================================================
# 1. TENANCY
# ==================================================================================
async def test_rank_never_returns_another_orgs_numbers_or_uses_its_rate_card(sms_app, session):
    """Org B has a dirt-cheap twilio number and a twilio rate override. Org A must never
    see the number, the provider, or the price - and a stale org-B session context must
    not change the answer."""
    client, registry, _carriers = sms_app
    _ta, org_a = await _org(
        client, "ten-a@example.com", [("+12145550401", "bandwidth"), ("+19725550401", "telnyx")]
    )
    _tb, org_b = await _org(
        client, "ten-b@example.com", [("+14155550402", "twilio"), ("+13035550402", "bandwidth")]
    )
    a_id, b_id = uuid.UUID(org_a["id"]), uuid.UUID(org_b["id"])

    # Org B prices twilio at almost nothing, and marks its own bandwidth account active.
    set_org_context(session, b_id)
    session.add(
        ProviderRate(
            id=uuid.uuid4(), org_id=b_id, provider="twilio", metric="sms_out", unit_cost_micros=1
        )
    )
    session.add(
        ProviderRate(
            id=uuid.uuid4(),
            org_id=b_id,
            provider="bandwidth",
            metric="sms_out",
            unit_cost_micros=1,
        )
    )
    await session.commit()

    # Deliberately leave the session pointed at org B before ranking for org A.
    a_candidates = await smart_routing.rank_routes(session, a_id, kind="sms", registry=registry)

    assert {c.provider for c in a_candidates} == {"bandwidth", "telnyx"}
    assert {c.e164 for c in a_candidates} == {"+12145550401", "+19725550401"}
    assert "+14155550402" not in {c.e164 for c in a_candidates}
    # Org B's 1-micro override must not have reached org A's cost model.
    assert all(c.cost_micros > 1 for c in a_candidates), [
        (c.provider, c.cost_micros) for c in a_candidates
    ]

    b_candidates = await smart_routing.rank_routes(session, b_id, kind="sms", registry=registry)
    assert {c.e164 for c in b_candidates} == {"+14155550402", "+13035550402"}
    assert all(c.cost_micros == 1 for c in b_candidates)


async def test_provider_account_suspension_is_scoped_to_the_org_that_suspended_it(
    sms_app, session
):
    client, registry, _carriers = sms_app
    _ta, org_a = await _org(
        client, "ten-c@example.com", [("+12145550403", "bandwidth"), ("+19725550403", "telnyx")]
    )
    _tb, org_b = await _org(
        client, "ten-d@example.com", [("+12145550404", "bandwidth"), ("+19725550404", "telnyx")]
    )
    a_id, b_id = uuid.UUID(org_a["id"]), uuid.UUID(org_b["id"])

    set_org_context(session, b_id)
    session.add(
        ProviderAccount(
            id=uuid.uuid4(), org_id=b_id, provider="telnyx", label="b-telnyx",
            credentials_encrypted="x", status="suspended",
        )
    )
    await session.commit()

    a_providers = {
        c.provider
        for c in await smart_routing.rank_routes(session, a_id, kind="sms", registry=registry)
    }
    b_providers = {
        c.provider
        for c in await smart_routing.rank_routes(session, b_id, kind="sms", registry=registry)
    }
    assert a_providers == {"bandwidth", "telnyx"}, "org B's suspension must not touch org A"
    assert b_providers == {"bandwidth"}, "org B's own suspension must exclude telnyx for B"


async def test_ranking_reads_no_unmapped_count_shape(sms_app, session, query_counter):
    """D42: every statement ranking issues counts/selects a MAPPED column, never
    `count(*) FROM <model>` without an org predicate."""
    client, registry, _carriers = sms_app
    _t, org = await _org(
        client, "ten-e@example.com", [("+12145550405", "bandwidth"), ("+19725550405", "telnyx")]
    )
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    query_counter.reset()
    await smart_routing.rank_routes(session, org_id, kind="sms", registry=registry)

    counts = [
        " ".join(s.split()).lower() for s in query_counter.statements if "count(" in s.lower()
    ]
    assert counts, (
        "ranking issued no aggregate at all - with two candidates it must run the "
        "reputation aggregate, so this probe is not measuring what it claims"
    )
    for flat in counts:
        assert "org_id" in flat, f"unscoped aggregate issued by ranking: {flat}"

    # And every SELECT that touched a tenant table carried the org predicate.
    tenant_selects = [
        " ".join(s.split()).lower()
        for s in query_counter.statements
        if s.lstrip().lower().startswith("select")
        and any(
            t in s.lower()
            for t in ("org_numbers", "provider_accounts", "provider_rates", "messages")
        )
    ]
    assert tenant_selects, "ranking issued no tenant-table selects at all - probe is wrong"
    assert all("org_id" in s for s in tenant_selects), tenant_selects


# ==================================================================================
# 2. DETERMINISM
# ==================================================================================
async def test_ranking_is_identical_across_20_calls_and_ties_break_by_provider_name(
    sms_app, session
):
    client, registry, _carriers = sms_app
    _t, org = await _org(
        client,
        "det@example.com",
        [
            ("+19725550501", "telnyx"),
            ("+12145550501", "bandwidth"),
            ("+14155550501", "twilio"),
            ("+13035550501", "signalwire"),
        ],
    )
    org_id = uuid.UUID(org["id"])

    # Force a four-way tie on cost so ONLY the tie-break can decide the order.
    set_org_context(session, org_id)
    for provider in ("telnyx", "bandwidth", "twilio", "signalwire"):
        session.add(
            ProviderRate(
                id=uuid.uuid4(),
                org_id=org_id,
                provider=provider,
                metric="sms_out",
                unit_cost_micros=5000,
            )
        )
    await session.commit()

    runs = []
    for _ in range(20):
        candidates = await smart_routing.rank_routes(
            session, org_id, kind="sms", registry=registry
        )
        runs.append([(c.provider, c.e164, c.score) for c in candidates])

    assert all(r == runs[0] for r in runs), "ranking is not deterministic across 20 calls"
    assert [p for p, _e, _s in runs[0]] == ["bandwidth", "signalwire", "telnyx", "twilio"]
    assert len({s for _p, _e, s in runs[0]}) == 1, "the tie was not actually a tie"


# ==================================================================================
# 3. REPUTATION FILTER - through the real send entry points
# ==================================================================================
async def _breach_number(client, session, token, org_id, e164: str) -> None:
    """Put `e164` into a spam-class breach by way of a real rejected send."""
    h = auth_headers(token, org_id)
    r = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "from": e164, "body": "hi"}, headers=h
    )
    assert r.status_code == 201, r.text
    set_org_context(session, org_id)
    message = await session.get(Message, uuid.UUID(r.json()["id"]))
    message.status = "rejected"
    message.error_code = "4750"  # spam-class
    message.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.commit()


async def test_reply_send_penalises_but_keeps_a_breached_number(sms_app, session):
    """1:1 path: the REAL plan_route must keep the breached number available and rank it
    below the clean one."""
    client, registry, _carriers = sms_app
    bad, clean = "+12145550601", "+19725550601"
    token, org = await _org(client, "rep-a@example.com", [(bad, "bandwidth"), (clean, "telnyx")])
    org_id = uuid.UUID(org["id"])
    await _breach_number(client, session, token, org_id, bad)

    set_org_context(session, org_id)
    plan = await routing_svc.plan_route(
        session, org_id, registry, contact_e164="+19725558888"
    )
    all_numbers = [r.from_e164 for r in plan.all_routes()]
    assert plan.primary.from_e164 == clean, "a breached number must not win the 1:1 ranking"
    assert bad in all_numbers, "a breach only PENALISES a 1:1 reply; it must stay available"


async def test_campaign_send_does_not_exclude_a_breached_number_in_production(
    app_with_carrier, session
):
    """FLIPPED IN P28 (D43). The finding this probe used to pin is FIXED.

    What it pinned: `services/outbound.py::outbound_tick` called `send_message(plan=None)`,
    so a campaign never consulted routing at all - `rank_routes(is_campaign=True)` was
    unreachable in production, a number in a spam-class breach still carried campaign
    traffic, and no route sentence was recorded for a campaign send either.

    What it asserts now, both halves:
      1. the breached number is EXCLUDED from bulk traffic and the clean one carries the
         campaign (a breach only PENALISES a 1:1 reply - see the probe above - and that
         asymmetry is the whole rule);
      2. the send records its `route_reason` sentence, exactly as a 1:1 send does.
    """
    client, fake, application = app_with_carrier
    bad, clean = "+12145550602", "+19725550602"
    token, org, _ = await make_org_with_number(client, "rep-b@example.com", "Org Rep", bad)
    org_id = uuid.UUID(org["id"])
    r = await client.post(
        "/api/v1/numbers", json={"e164": clean}, headers=auth_headers(token, org_id)
    )
    assert r.status_code == 201, r.text
    await _breach_number(client, session, token, org_id, bad)
    sent_before = len(fake.sent)

    set_org_context(session, org_id)
    lst = ContactList(
        id=uuid.uuid4(), org_id=org_id, name="L", source_filename="l.csv", status="ready",
        total_rows=1, accepted_count=1,
    )
    session.add(lst)
    await session.flush()
    contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name="c")
    session.add(contact)
    await session.flush()
    session.add(
        ContactListRow(
            id=uuid.uuid4(), org_id=org_id, list_id=lst.id, row_number=1,
            raw={"phone": CONTACT}, e164=CONTACT, contact_id=contact.id, status="accepted",
        )
    )
    await session.commit()

    campaign = await outbound_svc.create_campaign(
        session, org_id, name="C", channel="sms", list_id=lst.id, body="Hello",
        rate_per_minute=600, daily_cap=200, respect_warmup=False,
        max_attempts=2, retry_backoff_minutes=240,
    )
    await outbound_svc.enqueue_campaign_rows(session, campaign)
    await outbound_svc.start_campaign(session, campaign)

    counts = await outbound_svc.outbound_tick(
        session, fake, None, Random(1), registry=application.state.carriers
    )
    assert counts["sent"] == 1, counts
    assert fake.sent[-1].from_ == clean, (
        "D43 FIXED: a breached number must not carry campaign traffic - the clean number "
        "does"
    )
    assert all(m.from_ != bad for m in fake.sent[sent_before:]), (
        "nothing at all may leave the breached number once the campaign starts"
    )
    set_org_context(session, org_id)
    sent = (
        await session.execute(
            sa.select(Message)
            .where(Message.to_e164 == CONTACT, Message.direction == "outbound")
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    assert sent.route_reason, (
        "D43 FIXED, second half: a campaign send now goes through plan_route, so it "
        "records the same customer-facing route sentence a 1:1 send does"
    )
    assert sent.route_reason.startswith("Sent via"), sent.route_reason


async def test_campaign_with_only_a_breached_number_sends_nothing(app_with_carrier, session):
    """The other side of the D43 rule: when the ONLY number is in breach there is nothing
    to fail over to, so the campaign waits rather than sending from it anyway. The row
    stays queued (retryable) - a reputation breach clears on its own as the window rolls,
    so failing the row terminally would be wrong."""
    client, fake, application = app_with_carrier
    bad = "+12145550603"
    token, org, _ = await make_org_with_number(client, "rep-c@example.com", "Org RepC", bad)
    org_id = uuid.UUID(org["id"])
    await _breach_number(client, session, token, org_id, bad)
    sent_before = len(fake.sent)

    set_org_context(session, org_id)
    lst = ContactList(
        id=uuid.uuid4(), org_id=org_id, name="L", source_filename="l.csv", status="ready",
        total_rows=1, accepted_count=1,
    )
    session.add(lst)
    await session.flush()
    contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name="c")
    session.add(contact)
    await session.flush()
    session.add(
        ContactListRow(
            id=uuid.uuid4(), org_id=org_id, list_id=lst.id, row_number=1,
            raw={"phone": CONTACT}, e164=CONTACT, contact_id=contact.id, status="accepted",
        )
    )
    await session.commit()

    campaign = await outbound_svc.create_campaign(
        session, org_id, name="C", channel="sms", list_id=lst.id, body="Hello",
        from_numbers=[bad], rate_per_minute=600, daily_cap=200, respect_warmup=False,
        max_attempts=2, retry_backoff_minutes=240,
    )
    await outbound_svc.enqueue_campaign_rows(session, campaign)
    await outbound_svc.start_campaign(session, campaign)

    counts = await outbound_svc.outbound_tick(
        session, fake, None, Random(1), registry=application.state.carriers
    )
    assert counts["sent"] == 0, counts
    assert len(fake.sent) == sent_before, "a breached-only pool must send nothing at all"


# ==================================================================================
# 4. PINNED CARRIER SEMANTICS - sms and voice
# ==================================================================================
async def _pin(session, org_id, *, carrier: str, cross: bool) -> RoutingPolicy:
    set_org_context(session, org_id)
    policy = await routing_svc.get_policy(session, org_id)
    policy.pinned_carrier = carrier
    policy.allow_cross_carrier_failover = cross
    policy.allow_intra_carrier_failover = True
    policy.smart_routing = True
    await session.commit()
    return policy


@pytest.mark.parametrize("cross", [False, True])
async def test_sms_pin_first_and_walk_stops_after_the_pin_unless_cross_is_on(
    sms_app, session, cross
):
    """FINDING (non-blocking, pre-existing): rank_routes implements the spec's pin rule
    ("pinned first, others only as failover when allowed"), but the SMS PLAN never sees
    the others. `plan_route` builds its candidate list from
    `order = [policy.pinned_carrier]` (router.py:245, pre-P21), so under a pin the SMS
    walk stops after the pin whatever `allow_cross_carrier_failover` says. Voice, which
    consumes rank_routes directly, DOES continue. This probe pins both halves."""
    client, registry, _carriers = sms_app
    pinned_num = "+12145550700" if cross else "+12145550701"
    other_num = "+19725550700" if cross else "+19725550701"
    email = f"pin-sms-{int(cross)}@example.com"
    _t, org = await _org(client, email, [(pinned_num, "bandwidth"), (other_num, "telnyx")])
    org_id = uuid.UUID(org["id"])
    policy = await _pin(session, org_id, carrier="bandwidth", cross=cross)

    set_org_context(session, org_id)
    ranked = await smart_routing.rank_routes(
        session, org_id, kind="sms", registry=registry, policy=policy
    )
    assert ranked[0].provider == "bandwidth" and ranked[0].is_pinned is True
    if cross:
        assert "telnyx" in [c.provider for c in ranked], "ranking keeps the cross-carrier option"
    else:
        assert {c.provider for c in ranked} == {"bandwidth"}, "ranking drops it when cross is off"

    plan = await routing_svc.plan_route(session, org_id, registry, contact_e164="+19725557777")
    assert plan.primary.carrier_name == "bandwidth", "the pin must be attempted first"
    fallback_carriers = [r.carrier_name for r in plan.fallbacks]
    if cross:
        assert "telnyx" in fallback_carriers, (
            "N2 fixed (Fable 2026-09-10): with cross-provider failover on, the SMS plan keeps "
            "the other providers as fallbacks after the pin, like rank_routes and voice"
        )
    else:
        assert fallback_carriers == [], "cross-provider failover off: pin only"


@pytest.mark.parametrize("cross", [False, True])
async def test_voice_pin_walk_stops_after_the_pin_unless_cross_is_on(voice_app, session, cross):
    client, _registry, bandwidth, telnyx = voice_app
    pinned_num = "+12145550710" if cross else "+12145550711"
    other_num = "+19725550710" if cross else "+19725550711"
    email = f"pin-voice-{int(cross)}@example.com"
    token, org = await _org(client, email, [(pinned_num, "bandwidth"), (other_num, "telnyx")])
    org_id = uuid.UUID(org["id"])
    await _pin(session, org_id, carrier="bandwidth", cross=cross)

    bandwidth.scripted_results = [
        CreateCallResult("rejected", None, "credentials rejected (401)",
                         CarrierError("auth", "401", retryable=False))
    ]
    r = await client.post(
        "/api/v1/calls",
        json={"to": CONTACT, "from": pinned_num},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text

    assert len(bandwidth.create_calls) == 1, "the pin is always the first attempt"
    if cross:
        assert len(telnyx.create_calls) == 1, "cross ON: the voice walk must continue"
        assert r.json()["status"] != "failed"
    else:
        assert telnyx.create_calls == [], "cross OFF: the voice walk must stop after the pin"
        assert r.json()["status"] == "failed"


# ==================================================================================
# 5. POLICY 422 + NO MUTATION ON REJECTION
# ==================================================================================
async def test_policy_patch_422_leaves_the_row_byte_identical(sms_app, session):
    client, _registry, _carriers = sms_app
    token, org = await _org(client, "policy@example.com", [("+12145550800", "bandwidth")])
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)

    before = (await client.get("/api/v1/routing/policy", headers=h)).json()
    assert before["smart_routing"] is True
    assert before["pinned_carrier"] is None and before["preference"] == []

    r = await client.patch(
        "/api/v1/routing/policy", json={"smart_routing": False}, headers=h
    )
    assert r.status_code == 422, r.text

    after = (await client.get("/api/v1/routing/policy", headers=h)).json()
    assert after == before, "a REFUSED patch must leave the policy untouched"

    # And the DB row itself, not just the serialiser.
    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(RoutingPolicy).where(RoutingPolicy.org_id == org_id))
    ).scalar_one()
    await session.refresh(row)
    assert row.smart_routing is True

    # With something to route by, the same patch is accepted.
    ok = await client.patch(
        "/api/v1/routing/policy",
        json={"pinned_carrier": "bandwidth", "smart_routing": False},
        headers=h,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["smart_routing"] is False


# ==================================================================================
# 6. ROUTE_REASON VOCABULARY + never naming an undialled provider
# ==================================================================================
BANNED = ("carrier", "breaker", "dlr", "e.164", "e164", "registry", "score", "candidate", "none")


def _assert_plain(sentence: str) -> None:
    lowered = sentence.lower()
    for word in BANNED:
        assert word not in lowered, f"engineering word {word!r} leaked into: {sentence!r}"


async def test_sms_route_reason_is_plain_and_names_only_dialled_providers(sms_app, session):
    client, registry, carriers = sms_app
    first, second = "+12145550900", "+19725550900"
    token, org = await _org(
        client, "reason@example.com", [(first, "bandwidth"), (second, "telnyx")]
    )
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)

    from app.providers.domain import SendResult

    # Price telnyx above bandwidth so bandwidth is unambiguously the primary; the walk
    # then has to fail over TO telnyx, which is the sentence under test.
    set_org_context(session, org_id)
    session.add(
        ProviderRate(
            id=uuid.uuid4(), org_id=org_id, provider="telnyx", metric="sms_out",
            unit_cost_micros=90000,
        )
    )
    await session.commit()

    carriers["bandwidth"].scripted = [
        SendResult("rejected", None, CarrierError("auth", "401", retryable=False))
    ]

    r = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "body": "hi"}, headers=h
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "accepted"
    assert body["from_e164"] == second, "the winning number is the fallback's"
    set_org_context(session, org_id)
    stored = await session.get(Message, uuid.UUID(body["id"]))
    assert stored.carrier == "telnyx", "the send failed over"
    reason = body["route_reason"]
    assert reason == "Failed over to Telnyx — Bandwidth unavailable", reason
    _assert_plain(reason)

    for provider in ("Twilio", "SignalWire", "Plivo"):
        assert provider not in reason, f"{reason!r} names a provider that was never dialled"
    assert len(carriers["bandwidth"].sent) == 1 and len(carriers["telnyx"].sent) == 1
    assert carriers["twilio"].sent == [] and carriers["signalwire"].sent == []

    # The happy path names only the provider that sent it.
    r2 = await client.post(
        "/api/v1/messages", json={"to": "+19725556666", "from": second, "body": "hi"}, headers=h
    )
    assert r2.status_code == 201, r2.text
    assert (r2.json()["route_reason"] or "").startswith("Sent via Telnyx"), r2.json()
    _assert_plain(r2.json()["route_reason"] or "")
    assert "Bandwidth" not in (r2.json()["route_reason"] or ""), (
        "a send that never touched bandwidth must not name it"
    )


def test_every_route_sentence_shape_is_plain_english():
    shapes = []
    for kind in ("sms", "voice"):
        for pinned in (True, False):
            for reasons in ((), ("preferred",), ("only",), ("unpriced",), ("manual",),
                            ("cross_carrier",)):
                for health in ("closed", "half_open"):
                    candidate = smart_routing.RouteCandidate(
                        provider="telnyx", number_id=None, e164="+19725550001", score=1.0,
                        reasons=reasons, health_state=health, cost_micros=0, is_pinned=pinned,
                    )
                    shapes.append(smart_routing.route_sentence(candidate, kind=kind))
                    shapes.append(
                        smart_routing.route_sentence(
                            candidate, kind=kind, failed_over_from="bandwidth"
                        )
                    )
    assert shapes
    for sentence in shapes:
        _assert_plain(sentence)
        assert len(sentence) <= 255
        assert "+1" not in sentence, f"a phone number leaked into {sentence!r}"


def test_livekit_trunk_sentence_is_plain():
    _assert_plain(smart_routing.LIVEKIT_TRUNK_REASON)
    assert smart_routing.LIVEKIT_TRUNK_REASON == "Via your calling trunk"


# ==================================================================================
# 7. LIVEKIT PATH
# ==================================================================================
async def test_room_call_reason_is_derived_and_the_column_stays_null(session):
    """The room branch must report the trunk sentence WITHOUT writing (or committing) it -
    that write is the W11 race the supervisor removed."""
    from app.voice_plane import service as voice_service
    from app.voice_plane.livekit_api import LiveKitApi
    from tests.conftest import WEBHOOK_PASS, WEBHOOK_USER
    from tests.test_voice_plane import (
        default_lk_handler,
        make_livekit_settings,
        make_org_with_room_number,
        mock_livekit_client,
    )
    from tests.test_voice_webhooks import install_voice_carrier

    settings = make_livekit_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    install_voice_carrier(application, FakeVoiceCarrier())
    requests: list[httpx.Request] = []
    lk_client = mock_livekit_client(default_lk_handler(requests))
    application.state.livekit = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key="lk-test-key",
        api_secret="lk-test-secret-value-padded-to-32-bytes-plus", client=lk_client,
    )
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token, org, _ = await make_org_with_room_number(
            client, "p21-room@example.com", "Org Room", "+12145551000"
        )
        h = auth_headers(token, org["id"])
        r = await client.post("/api/v1/calls", json={"to": CONTACT, "via": "room"}, headers=h)
        assert r.status_code == 201, r.text
        assert r.json()["route_reason"] == smart_routing.LIVEKIT_TRUNK_REASON

        call_id = uuid.UUID(r.json()["id"])
        await voice_service.wait_for_pending_dial_tasks()

        set_org_context(session, uuid.UUID(org["id"]))
        row = await session.get(Call, call_id)
        await session.refresh(row)
        assert row.route_reason is None, (
            "the room branch must NOT write route_reason - it is derived (W11 race)"
        )
        assert (row.extra or {}).get("via") == "livekit"
    await lk_client.aclose()


def test_room_branch_contains_no_commit():
    """Source-level guard on the supervisor's race fix."""
    import inspect

    from app.api.routes import calls as calls_routes

    src = inspect.getsource(calls_routes.create_call)
    room_branch = src.split('if payload.via == "room":', 1)[1].split("registry = getattr(", 1)[0]
    assert "commit()" not in room_branch, room_branch
    assert "route_reason" not in room_branch


# ==================================================================================
# 8. DEFAULTS
# ==================================================================================
async def test_new_org_gets_smart_routing_and_cross_failover_on(client, session):
    token, org, _ = await make_org_with_number(
        client, "def-a@example.com", "Org Def", "+12145551100"
    )
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    policy = (
        await session.execute(sa.select(RoutingPolicy).where(RoutingPolicy.org_id == org_id))
    ).scalar_one()
    assert policy.smart_routing is True
    assert policy.allow_cross_carrier_failover is True


async def test_reseeding_leaves_an_existing_policy_untouched(client, session):
    token, org, _ = await make_org_with_number(
        client, "def-b@example.com", "Org Def2", "+12145551101"
    )
    org_id = uuid.UUID(org["id"])
    set_org_context(session, org_id)
    policy = (
        await session.execute(sa.select(RoutingPolicy).where(RoutingPolicy.org_id == org_id))
    ).scalar_one()
    policy.smart_routing = False
    policy.pinned_carrier = "telnyx"
    policy.allow_cross_carrier_failover = False
    await session.commit()
    policy_id = policy.id

    report = await defaults_svc.seed_org_defaults(session, org_id)
    await session.commit()
    assert "routing_policy" in report["existing"]
    assert "routing_policy" not in report["created"]

    rows = (
        await session.execute(sa.select(RoutingPolicy).where(RoutingPolicy.org_id == org_id))
    ).scalars().all()
    assert len(rows) == 1, "re-seeding must not duplicate the policy row"
    assert rows[0].id == policy_id
    assert rows[0].smart_routing is False
    assert rows[0].pinned_carrier == "telnyx"
    assert rows[0].allow_cross_carrier_failover is False


# ==================================================================================
# 9. BREAKER FEEDING - telnyx AND bandwidth voice adapters
# ==================================================================================
class StubResponse:
    def __init__(self, status_code: int, text: str = "", payload: dict | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class StubClient:
    def __init__(self, response: StubResponse) -> None:
        self.response = response

    async def post(self, *args, **kwargs) -> StubResponse:
        return self.response


def _telnyx_harness(client: StubClient):
    from app.providers.telnyx.voice import TelnyxVoiceMixin

    class H(TelnyxVoiceMixin):
        name = "telnyx"
        base_url = "https://api.telnyx.com/v2"
        api_key = "k"
        voice_connection_id = "c"
        _public_key = ""

        async def _get_client(self):
            return client

    return H()


def _bandwidth_harness(client: StubClient):
    from app.providers.bandwidth.voice import BandwidthVoiceMixin

    class H(BandwidthVoiceMixin):
        name = "bandwidth"
        account_id = "acct"
        application_id = "app"
        voice_application_id = "app"
        voice_callback_url = "https://example.com/voice/"

        async def _get_client(self):
            return client

        async def auth_kwargs(self) -> dict:
            return {}

    return H()


@pytest.mark.parametrize("provider", ["telnyx", "bandwidth"])
async def test_voice_auth_error_opens_the_breaker(provider: str):
    payloads = {
        "telnyx": {"errors": [{"code": "401", "detail": "bad creds"}]},
        "bandwidth": {"type": "401", "description": "bad creds"},
    }
    response = StubResponse(401, "bad creds", payloads[provider])
    harness = _telnyx_harness(StubClient(response)) if provider == "telnyx" else (
        _bandwidth_harness(StubClient(response))
    )
    result = await harness.create_call(to=CONTACT, from_="+12145550100")

    assert result.status == "rejected"
    assert result.error is not None, f"{provider} voice rejection carries no CarrierError"
    assert result.error.category == "auth"
    assert opens_breaker(result.error) is True, "P14 DR-1: auth IS a carrier fault"

    # And the breaker really does move when fed this error.
    health = HealthRegistry(clock=FakeClock())
    breaker = health.breaker(provider)
    for _ in range(10):
        breaker.record_failure(result.error)
    assert breaker.state() == "open"


@pytest.mark.parametrize("provider", ["telnyx", "bandwidth"])
async def test_voice_invalid_request_does_not_open_the_breaker(provider: str):
    payloads = {
        "telnyx": {"errors": [{"code": "40001", "detail": "Invalid request"}]},
        "bandwidth": {"type": "4302", "description": "Invalid request"},
    }
    response = StubResponse(400, "Invalid request", payloads[provider])
    harness = _telnyx_harness(StubClient(response)) if provider == "telnyx" else (
        _bandwidth_harness(StubClient(response))
    )
    result = await harness.create_call(to=CONTACT, from_="+12145550100")

    assert result.status == "rejected"
    assert result.error is not None
    assert result.error.category == "invalid_request"
    assert opens_breaker(result.error) is False

    health = HealthRegistry(clock=FakeClock())
    breaker = health.breaker(provider)
    for _ in range(10):
        breaker.record_failure(result.error)
    assert breaker.state() == "closed", "an invalid_request must never open a breaker"


async def test_invalid_request_on_a_real_voice_dial_stops_the_walk_and_leaves_breaker_closed(
    voice_app, session
):
    """The adapter taxonomy wired all the way through the real dial path."""
    client, registry, bandwidth, telnyx = voice_app
    token, org = await _org(
        client, "walkstop@example.com", [("+12145551200", "bandwidth"), ("+19725551200", "telnyx")]
    )
    bandwidth.scripted_results = [
        CreateCallResult("rejected", None, "Invalid request",
                         CarrierError("invalid_request", "40001", retryable=False))
    ]
    r = await client.post(
        "/api/v1/calls",
        json={"to": CONTACT, "from": "+12145551200"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "failed"
    assert len(bandwidth.create_calls) == 1
    assert telnyx.create_calls == [], "our own bad request must not be spread to another provider"
    assert registry.health.breaker("bandwidth").state() == "closed"


# ==================================================================================
# 10. ORDERING SMELL: rank_routes reads the policy BEFORE binding the org context
# ==================================================================================
async def test_rank_routes_binds_the_org_itself_before_reading_the_policy(
    sms_app, session
):
    """FINDING (non-blocking): `rank_routes` calls `get_policy` (a tenant-scoped SELECT)
    BEFORE its own `set_org_context`, so a caller that has not already bound the session
    gets MissingTenantContextError rather than a ranking. Harmless today - both production
    call sites pass `policy=` explicitly - but the guard clause is in the wrong order."""
    from app.errors import MissingTenantContextError

    client, registry, _carriers = sms_app
    _t, org = await _org(
        client, "ctx@example.com", [("+12145551300", "bandwidth"), ("+19725551300", "telnyx")]
    )
    org_id = uuid.UUID(org["id"])

    # N3 fixed (Fable 2026-09-10): rank_routes binds the org itself before any read.
    set_org_context(session, None)
    ranked = await smart_routing.rank_routes(session, org_id, kind="sms", registry=registry)
    assert ranked, "rank_routes must bind the org and rank, not raise"
    assert MissingTenantContextError  # still importable; kept for the narrative above

    # With an explicit policy (what production passes) the same call is fine.
    set_org_context(session, org_id)
    policy = await routing_svc.get_policy(session, org_id)
    await session.commit()
    set_org_context(session, None)
    candidates = await smart_routing.rank_routes(
        session, org_id, kind="sms", registry=registry, policy=policy
    )
    assert {c.provider for c in candidates} == {"bandwidth", "telnyx"}


# ==================================================================================
# 11. UNIFIED TIMELINE carries route_reason (Fable's conversations.py patch)
# ==================================================================================
async def test_timeline_message_event_carries_the_stored_route_reason(sms_app, session):
    client, _registry, carriers = sms_app
    ours = "+12145551400"
    token, org = await _org(client, "tl-msg@example.com", [(ours, "bandwidth")])
    org_id = uuid.UUID(org["id"])
    h = auth_headers(token, org_id)

    r = await client.post(
        "/api/v1/messages", json={"to": CONTACT, "from": ours, "body": "hi"}, headers=h
    )
    assert r.status_code == 201, r.text
    message_id = r.json()["id"]
    reason = r.json()["route_reason"]
    assert reason, "the real send path must write a sentence"

    set_org_context(session, org_id)
    stored = await session.get(Message, uuid.UUID(message_id))
    assert stored.route_reason == reason

    t = await client.get(
        f"/api/v1/conversations/{CONTACT}/timeline", params={"our_e164": ours}, headers=h
    )
    assert t.status_code == 200, t.text
    events = [i for i in t.json()["items"] if i["kind"] == "message" and i["id"] == message_id]
    assert len(events) == 1, t.json()
    assert events[0]["route_reason"] == stored.route_reason
    _assert_plain(events[0]["route_reason"])
    assert len(carriers["bandwidth"].sent) == 1


async def test_timeline_room_call_event_shows_the_trunk_sentence_with_a_null_column(session):
    from app.voice_plane import service as voice_service
    from app.voice_plane.livekit_api import LiveKitApi
    from tests.conftest import WEBHOOK_PASS, WEBHOOK_USER
    from tests.test_voice_plane import (
        default_lk_handler,
        make_livekit_settings,
        make_org_with_room_number,
        mock_livekit_client,
    )
    from tests.test_voice_webhooks import install_voice_carrier

    settings = make_livekit_settings(
        bandwidth_webhook_username=WEBHOOK_USER, bandwidth_webhook_password=WEBHOOK_PASS
    )
    application = create_app(settings)
    install_voice_carrier(application, FakeVoiceCarrier())
    requests: list[httpx.Request] = []
    lk_client = mock_livekit_client(default_lk_handler(requests))
    application.state.livekit = LiveKitApi(
        url="ws://127.0.0.1:7880", api_key="lk-test-key",
        api_secret="lk-test-secret-value-padded-to-32-bytes-plus", client=lk_client,
    )
    ours = "+12145551401"
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token, org, _ = await make_org_with_room_number(
            client, "tl-room@example.com", "Org TL Room", ours
        )
        h = auth_headers(token, org["id"])
        r = await client.post("/api/v1/calls", json={"to": CONTACT, "via": "room"}, headers=h)
        assert r.status_code == 201, r.text
        call_id = r.json()["id"]
        await voice_service.wait_for_pending_dial_tasks()

        t = await client.get(
            f"/api/v1/conversations/{CONTACT}/timeline", params={"our_e164": ours}, headers=h
        )
        assert t.status_code == 200, t.text
        events = [i for i in t.json()["items"] if i["kind"] == "call" and i["id"] == call_id]
        assert len(events) == 1, t.json()
        assert events[0]["route_reason"] == smart_routing.LIVEKIT_TRUNK_REASON

        set_org_context(session, uuid.UUID(org["id"]))
        row = await session.get(Call, uuid.UUID(call_id))
        await session.refresh(row)
        assert row.route_reason is None, "the room branch must still store NOTHING"
    await lk_client.aclose()
