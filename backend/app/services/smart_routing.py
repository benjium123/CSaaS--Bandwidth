"""Automatic route ranking and the customer-facing explanation for a chosen route.

Ranking is deliberately side-effect free: it reads breaker state but never consumes the
single half-open probe token, and it never writes to reputation or spend. The route that
actually sends is the only thing that may mutate a breaker or an audit table.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.models import OrgNumber, ProviderAccount
from app.providers.voice import VoiceCarrier
from app.services import reputation as reputation_svc
from app.services import spend as spend_svc

LIVEKIT_TRUNK_REASON = "Via your calling trunk"

_PROVIDER_DISPLAY_NAMES = {
    "bandwidth": "Bandwidth",
    "telnyx": "Telnyx",
    "twilio": "Twilio",
    "plivo": "Plivo",
    "signalwire": "SignalWire",
}


def _probe_available(breaker) -> bool:  # noqa: ANN001 - providers.health.Breaker
    """Does this half-open breaker still hold its single probe token?

    READ-ONLY. ``Breaker.allows_send()`` answers the same question but CONSUMES the token,
    and ranking must never do that - the send path calls it moments later and would find
    the token already gone.
    """
    return not getattr(breaker, "_probing", False)


def _health_penalty(breaker, state: str) -> float:  # noqa: ANN001 - providers.health.Breaker
    """0.0 for a healthy route, 1.0 (i.e. -40 points) for a wounded one.

    The subtlety is half_open, and getting it wrong silently breaks carrier RECOVERY.
    A breaker that has cooled down is entitled to exactly ONE probe (P14 DR-3): that probe
    is how ``record_success`` ever fires and the breaker ever closes again. If ranking
    penalised it unconditionally, any healthy alternative would out-rank it forever, the
    walk would never reach it, the token would never be spent, and the carrier would stay
    demoted for the life of the process - permanently, not until it recovered.

    So: while the token is AVAILABLE the route ranks as healthy, precisely so the probe is
    spent. Once it is spent (a concurrent send took it), every other send in that window
    takes the full penalty - which is the "half-open is allowed, penalised" rule doing what
    it is actually for: preventing a thundering herd, not preventing recovery.
    """
    if state != "half_open":
        return 0.0
    return 0.0 if _probe_available(breaker) else 1.0


@dataclass(frozen=True)
class RouteCandidate:
    provider: str
    number_id: uuid.UUID | None
    e164: str
    score: float
    reasons: tuple[str, ...]
    health_state: str
    cost_micros: int
    is_pinned: bool


async def rank_routes(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    kind: str,
    registry,  # noqa: ANN001 - CarrierRegistry or org proxy
    from_number: str | None = None,
    to_e164: str = "",
    is_campaign: bool = False,
    policy=None,  # noqa: ANN001 - RoutingPolicy
    numbers=None,  # noqa: ANN001 - list[OrgNumber]
) -> list[RouteCandidate]:
    if registry is None:
        return []

    # Bind the org BEFORE any tenant-scoped read (Opus verify N3).
    set_org_context(session, org_id)

    if policy is None:
        from app.routing import router as routing_router

        policy = await routing_router.get_policy(session, org_id)

    if numbers is None:
        numbers = list(
            (
                await session.execute(
                    sa.select(OrgNumber)
                    .where(OrgNumber.is_active.is_(True), OrgNumber.status == "active")
                    .order_by(OrgNumber.e164)
                )
            ).scalars().all()
        )
    else:
        numbers = [n for n in numbers if n.is_active and n.status == "active"]

    if from_number is not None:
        requested = [n for n in numbers if n.e164 == from_number]
        if policy.allow_intra_carrier_failover or policy.allow_cross_carrier_failover:
            numbers = requested + [n for n in numbers if n.e164 != from_number]
        else:
            numbers = requested
    else:
        numbers = list(numbers)

    metric = {"sms": "sms_out", "mms": "mms_out", "voice": "voice_min_out"}.get(kind)
    if metric is None:
        return []

    account_status: dict[str, str] = {}
    for account in (await session.execute(sa.select(ProviderAccount))).scalars().all():
        account_status[account.provider] = account.status

    cands: dict[tuple[str, uuid.UUID], dict] = {}
    for number in numbers:
        provider = number.carrier
        if provider not in registry:
            continue
        carrier = registry.get(provider)
        if kind in ("sms", "mms"):
            if not carrier.capabilities.supports_messaging:
                continue
        elif kind == "voice":
            if not isinstance(carrier, VoiceCarrier):
                continue
        else:
            continue

        if provider in account_status and account_status[provider] != "active":
            continue

        breaker = registry.health.breaker(provider)
        state = breaker.state()
        if state == "open":
            continue

        key = (provider, number.id)
        cands[key] = {
            "provider": provider,
            "number_id": number.id,
            "e164": number.e164,
            "health_state": state,
            "health_penalty": _health_penalty(breaker, state),
            "reputation_penalty": 0.0,
            "normalised_cost": 0.0,
            "cost_micros": 0,
            "is_pinned": False,
            "preference_bonus": 0.0,
            "unpriced": False,
        }

    if not cands:
        return []

    if is_campaign or len(cands) > 1:
        # Reputation stats cannot change a single non-campaign candidate: there is
        # nothing to exclude and nothing to reorder, so the aggregate can be skipped.
        stats = await reputation_svc.compute_number_stats(session, org_id)
        stats_by_e164 = {s.e164: s for s in stats}
        for key, candidate in list(cands.items()):
            number_stats = stats_by_e164.get(candidate["e164"])
            if number_stats is not None and reputation_svc._breaches(number_stats):
                if is_campaign:
                    del cands[key]
                    continue
                candidate["reputation_penalty"] = 1.0

    if not cands:
        return []

    providers = {c["provider"] for c in cands.values()}
    rates: dict[str, tuple[int, bool, bool]] = {}
    for provider in providers:
        rates[provider] = await spend_svc.resolve_rate(session, provider, metric)

    known_costs = [cost for cost, _override, is_known in rates.values() if is_known]
    min_cost = min(known_costs) if known_costs else 0
    max_cost = max(known_costs) if known_costs else 0

    for candidate in cands.values():
        cost, _override, is_known = rates[candidate["provider"]]
        if not is_known:
            candidate["unpriced"] = True
            candidate["cost_micros"] = max_cost if known_costs else 0
            candidate["normalised_cost"] = 1.0
        else:
            candidate["cost_micros"] = cost
            if max_cost > min_cost:
                candidate["normalised_cost"] = (cost - min_cost) / (max_cost - min_cost)
            else:
                candidate["normalised_cost"] = 0.0

    if policy.pinned_carrier:
        pinned = policy.pinned_carrier
        for key, candidate in list(cands.items()):
            if candidate["provider"] == pinned:
                candidate["is_pinned"] = True
            elif not policy.allow_cross_carrier_failover:
                del cands[key]
        if not cands:
            return []

    preference = list(policy.preference or [])
    bonus_by_provider: dict[str, float] = {}
    if preference:
        length = len(preference)
        for index, name in enumerate(preference):
            bonus_by_provider[name] = (length - index) / length

    for candidate in cands.values():
        candidate["preference_bonus"] = bonus_by_provider.get(candidate["provider"], 0.0)

    route_objects: list[RouteCandidate] = []
    for candidate in cands.values():
        if policy.smart_routing is False:
            candidate["normalised_cost"] = 0.0
            candidate["reputation_penalty"] = 0.0

        reasons: list[str] = []
        if candidate["is_pinned"]:
            reasons.append("pinned")
        if candidate["preference_bonus"] > 0:
            reasons.append("preferred")
        if candidate["unpriced"]:
            reasons.append("unpriced")
        if policy.smart_routing is False:
            reasons.append("manual")
        if len(cands) == 1:
            reasons.append("only")

        score = (
            100.0
            - 40.0 * candidate["health_penalty"]
            - 30.0 * candidate["reputation_penalty"]
            - 20.0 * candidate["normalised_cost"]
            + 10.0 * candidate["preference_bonus"]
        )
        route_objects.append(
            RouteCandidate(
                provider=candidate["provider"],
                number_id=candidate["number_id"],
                e164=candidate["e164"],
                score=round(score, 4),
                reasons=tuple(reasons),
                health_state=candidate["health_state"],
                cost_micros=candidate["cost_micros"],
                is_pinned=candidate["is_pinned"],
            )
        )

    pinned = [c for c in route_objects if c.is_pinned]
    rest = [c for c in route_objects if not c.is_pinned]
    pinned.sort(key=lambda c: (-c.score, c.provider, c.e164))
    rest.sort(key=lambda c: (-c.score, c.provider, c.e164))
    ordered = pinned + rest

    if ordered:
        top_provider = ordered[0].provider
        ordered = [
            replace(c, reasons=c.reasons + ("cross_carrier",))
            if c.provider != top_provider
            else c
            for c in ordered
        ]

    return ordered


async def campaign_excluded_e164s(
    session: AsyncSession, org_id: uuid.UUID
) -> set[str]:
    """Numbers a CAMPAIGN must not send from right now (D43).

    ``rank_routes(is_campaign=True)`` already deletes these candidates, but the campaign
    runner picks its own sender out of the campaign's own number pool before any plan is
    built - so the exclusion has to be available as a plain set the runner can subtract
    from that pool, and as a filter ``plan_route`` applies to every one of its branches.
    Computing it once per campaign per tick (instead of once per row) is the whole reason
    it is a separate function: ``compute_number_stats`` is a trailing-7-day aggregate.

    The rule itself is unchanged from P21: a breach only PENALISES a 1:1 reply, but it
    EXCLUDES bulk traffic - a number the phone networks are already unhappy with must not
    be handed a campaign's volume.
    """
    set_org_context(session, org_id)
    stats = await reputation_svc.compute_number_stats(session, org_id)
    return {s.e164 for s in stats if reputation_svc._breaches(s)}


def route_sentence(
    candidate: RouteCandidate,
    *,
    kind: str,
    failed_over_from: str | None = None,
    only_route: bool = False,
) -> str:
    display = _PROVIDER_DISPLAY_NAMES.get(candidate.provider, candidate.provider.title())

    if failed_over_from is not None:
        previous = _PROVIDER_DISPLAY_NAMES.get(failed_over_from, failed_over_from.title())
        sentence = f"Failed over to {display} — {previous} unavailable"
        return sentence[:255]

    prefix = f"Called via {display}" if kind == "voice" else f"Sent via {display}"

    if candidate.is_pinned:
        suffix = "your chosen provider"
    elif "preferred" in candidate.reasons:
        suffix = "your preferred provider"
    elif only_route or "only" in candidate.reasons:
        suffix = "your only route"
    elif candidate.health_state == "half_open":
        suffix = "the healthiest route available"
    else:
        suffix = "cheapest healthy route"

    sentence = f"{prefix} — {suffix}"
    return sentence[:255]
