"""Choosing which (number, carrier) pair sends a message.

The one idea this module exists to enforce: **routing does not pick a carrier, it picks a
number** (phase-3b DR-1). A DID is provisioned, 10DLC-registered and STIR/SHAKEN-attested at
exactly one carrier, so "fail over to Telnyx" is never transparent - it changes the sender
the recipient sees.

Everything below follows from that. Explicit choices are honoured or refused, never
silently substituted; health reorders candidates but cannot override an operator; and a
carrier switch is refused mid-conversation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance import registration
from app.errors import (
    CarrierNotConfiguredError,
    ComplianceBlockedError,
    ValidationFailedError,
)
from app.models import MessageThread, OrgNumber
from app.models.routing import RoutingPolicy
from app.providers.registry import CarrierRegistry
from app.services.sender import pick_deterministic

log = structlog.get_logger("routing")


@dataclass(frozen=True)
class Route:
    carrier_name: str
    from_e164: str
    #: Why this route was chosen. Carried into logs so a surprising send can be explained
    #: after the fact instead of reverse-engineered.
    reason: str


@dataclass(frozen=True)
class RoutePlan:
    primary: Route
    fallbacks: tuple[Route, ...] = ()

    def all_routes(self) -> tuple[Route, ...]:
        return (self.primary, *self.fallbacks)


async def get_policy(session: AsyncSession, org_id: uuid.UUID) -> RoutingPolicy:
    """Fetch or lazily create this org's policy, with defaults that change nothing.

    An org that has never touched routing must behave exactly as it did before this phase
    existed - so the defaults are "prefer the registry order, no cross-carrier failover".
    """
    row = (await session.execute(sa.select(RoutingPolicy).limit(1))).scalar_one_or_none()
    if row is None:
        row = RoutingPolicy(id=uuid.uuid4(), org_id=org_id, preference=[])
        session.add(row)
        await session.flush()
    return row


async def _active_numbers(session: AsyncSession, org_id: uuid.UUID) -> list[OrgNumber]:
    return list(
        (
            await session.execute(
                sa.select(OrgNumber)
                .where(OrgNumber.is_active.is_(True), OrgNumber.status == "active")
                .order_by(OrgNumber.e164)
            )
        ).scalars().all()
    )


def _carrier_order(policy: RoutingPolicy, registry: CarrierRegistry) -> list[str]:
    """Preference first (as configured), then anything else the registry has."""
    preferred = [c for c in (policy.preference or []) if c in registry]
    remainder = [c for c in registry.names() if c not in preferred]
    return preferred + remainder


async def plan_route(
    session: AsyncSession,
    org_id: uuid.UUID,
    registry: CarrierRegistry,
    *,
    contact_e164: str = "",
    requested_from: str | None = None,
    requested_carrier: str | None = None,
    thread_our_number: str | None = None,
    is_reply_in_thread: bool = False,
    require_registration: bool = False,
) -> RoutePlan:
    """Resolve the send to a primary route plus an ordered fallback list.

    Precedence (phase-3b DR-2), highest first:
      1. an explicit ``from`` - pins the carrier that owns that number
      2. an explicit ``carrier`` - the operator's "at will" override
      3. the thread's sticky sender - continuity beats optimisation
      4. the org's pinned carrier, then its preference order
      5. whatever the registry considers primary
    """
    if len(registry) == 0:
        raise CarrierNotConfiguredError("No carrier is configured for messaging")

    numbers = await _active_numbers(session, org_id)
    if not numbers:
        raise ValidationFailedError("This organisation has no active numbers to send from")

    # A number whose registration we KNOW is incomplete is removed before anything else
    # looks at it (phase-4-plan DR-1). Finding this out from a carrier rejection means the
    # violation is already on the brand's record.
    numbers, refused = await registration.partition_by_eligibility(
        session, numbers, require_registration=require_registration
    )
    if not numbers:
        raise ComplianceBlockedError(
            "; ".join(refused.values())
            or "No number on this organisation is registered to send"
        )

    by_e164 = {n.e164: n for n in numbers}
    policy = await get_policy(session, org_id)

    # ---- 1. explicit from --------------------------------------------------------
    if requested_from:
        number = by_e164.get(requested_from)
        if number is None:
            # Distinguish "not yours" from "yours but not allowed to send" - the second is
            # actionable and the operator needs to know which one they are looking at.
            if requested_from in refused:
                raise ComplianceBlockedError(refused[requested_from])
            raise ValidationFailedError(
                f"{requested_from} is not an active number on this organisation"
            )
        _require_usable(registry, number.carrier, explicit=True)
        return RoutePlan(Route(number.carrier, number.e164, "explicit_from"))

    # ---- 2. explicit carrier -----------------------------------------------------
    if requested_carrier:
        _require_usable(registry, requested_carrier, explicit=True)
        ordered = _spread(numbers, requested_carrier, contact_e164)
        if not ordered:
            blocked = [msg for e164, msg in refused.items() if e164 not in by_e164]
            if blocked:
                raise ComplianceBlockedError("; ".join(blocked))
            raise ValidationFailedError(
                f"No active number is hosted on {requested_carrier!r}"
            )
        return RoutePlan(
            Route(requested_carrier, ordered[0], "explicit_carrier"),
            tuple(
                Route(requested_carrier, e164, "explicit_carrier_failover")
                for e164 in ordered[1:]
                if policy.allow_intra_carrier_failover
            ),
        )

    # ---- 3. sticky sender --------------------------------------------------------
    if thread_our_number and thread_our_number in by_e164:
        number = by_e164[thread_our_number]
        if registry.get(number.carrier) is not None and registry.health.is_healthy(
            number.carrier
        ):
            return RoutePlan(
                Route(number.carrier, number.e164, "sticky_sender"),
                _fallbacks(
                    numbers, policy, registry, exclude={number.e164},
                    same_carrier=number.carrier, is_reply_in_thread=is_reply_in_thread,
                ),
            )
        # Sticky sender's carrier is down. Falling through is correct, but it is exactly
        # the moment the recipient may see a new sender - so it is logged loudly.
        log.warning(
            "sticky_sender_unavailable",
            carrier=number.carrier,
            number=number.e164,
            is_reply_in_thread=is_reply_in_thread,
        )

    # ---- 4/5. policy, then registry order ----------------------------------------
    order = [policy.pinned_carrier] if policy.pinned_carrier else _carrier_order(policy, registry)
    ranked: list[Route] = []
    for carrier_name in order:
        if carrier_name is None or registry.get(carrier_name) is None:
            continue
        # Within a carrier, keep P2's deterministic spread. Routing decides WHICH CARRIER;
        # collapsing every new conversation onto that carrier's first number would quietly
        # undo the spread that exists to stay under per-number velocity limits.
        for e164 in _spread(numbers, carrier_name, contact_e164):
            ranked.append(Route(carrier_name, e164, "policy"))

    # Health reorders; it never removes the last option, because refusing to send at all
    # is worse than trying a carrier whose breaker is open.
    healthy = [r for r in ranked if registry.health.is_healthy(r.carrier_name)]
    unhealthy = [r for r in ranked if not registry.health.is_healthy(r.carrier_name)]
    ranked = healthy + unhealthy

    if not ranked:
        raise CarrierNotConfiguredError(
            "No active number belongs to a configured carrier - check that each number's "
            "carrier has credentials"
        )

    primary = ranked[0]
    fallbacks = tuple(
        r
        for r in ranked[1:]
        if _failover_allowed(primary, r, policy, is_reply_in_thread=is_reply_in_thread)
    )
    return RoutePlan(primary, fallbacks)


def _require_usable(registry: CarrierRegistry, name: str, *, explicit: bool) -> None:
    """An explicit choice is honoured or refused - never quietly swapped.

    Silent substitution is how an operator discovers at 2am that half their traffic left on
    the wrong brand. If they named a carrier, they get it or they get an error.
    """
    if registry.get(name) is None:
        raise CarrierNotConfiguredError(
            f"Carrier {name!r} is not configured on this deployment"
        )
    if explicit and not registry.health.is_healthy(name):
        raise CarrierNotConfiguredError(
            f"Carrier {name!r} was explicitly requested but is currently failing. "
            "Retry, or choose another carrier deliberately."
        )


def _failover_allowed(
    primary: Route, candidate: Route, policy: RoutingPolicy, *, is_reply_in_thread: bool
) -> bool:
    if candidate.carrier_name == primary.carrier_name:
        return policy.allow_intra_carrier_failover
    if is_reply_in_thread:
        # A thread that has been spoken to keeps its sender or it does not get sent.
        # Silence is recoverable; a stranger answering someone's conversation is not.
        return False
    return policy.allow_cross_carrier_failover


def _fallbacks(
    numbers: list[OrgNumber],
    policy: RoutingPolicy,
    registry: CarrierRegistry,
    *,
    exclude: set[str],
    same_carrier: str,
    is_reply_in_thread: bool,
) -> tuple[Route, ...]:
    out: list[Route] = []
    for number in numbers:
        if number.e164 in exclude or registry.get(number.carrier) is None:
            continue
        if number.carrier == same_carrier:
            if policy.allow_intra_carrier_failover:
                out.append(Route(number.carrier, number.e164, "intra_carrier_failover"))
        elif policy.allow_cross_carrier_failover and not is_reply_in_thread:
            out.append(Route(number.carrier, number.e164, "cross_carrier_failover"))
    return tuple(out)


async def has_prior_conversation(
    session: AsyncSession, org_id: uuid.UUID, contact_e164: str
) -> bool:
    """Has this contact already been spoken to on ANY of our numbers?

    Threads are keyed by ``our_e164``, so sending from a different number does not continue
    a conversation - it starts a second one alongside it. That is the concrete harm DR-1
    guards against, and this is the check that detects it.
    """
    row = (
        await session.execute(
            sa.select(MessageThread.id)
            .where(
                MessageThread.contact_e164 == contact_e164,
                MessageThread.last_message_at.is_not(None),
            )
            .limit(1)
        )
    ).first()
    return row is not None


def _spread(numbers: list[OrgNumber], carrier_name: str, contact_e164: str) -> list[str]:
    """This carrier's numbers, best first, using P2's deterministic spread.

    Same contact always lands on the same number, so a conversation stays put without any
    stored state - and the pool is still shared evenly across contacts.
    """
    pool = sorted(n.e164 for n in numbers if n.carrier == carrier_name)
    if not pool:
        return []
    if not contact_e164:
        return pool
    chosen = pick_deterministic(contact_e164, pool)
    return [chosen] + [e for e in pool if e != chosen]
