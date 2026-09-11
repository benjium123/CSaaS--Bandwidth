from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

import phonenumbers
import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.auth.deps import OrgContext, require_permission
from app.compliance import registration
from app.errors import (
    CarrierNotConfiguredError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from app.models import AgentProfile, Inbox, OrgNumber, ProviderAccount
from app.models.callflow import CallFlow
from app.models.numbers import Campaign
from app.providers import numbers as numbers_api
from app.providers import registry_org
from app.services import agent as agent_svc
from app.services import audit as audit_svc
from app.services import flows as flows_svc
from app.services import provider_accounts as provider_accounts_svc
from app.services import reputation as reputation_svc
from app.services import telephony_billing

router = APIRouter(prefix="/api/v1/numbers", tags=["numbers"])


def to_e164(raw: str, region: str = "US") -> str:
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException as exc:
        raise ValidationFailedError(f"{raw!r} is not a parseable phone number") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValidationFailedError(f"{raw!r} is not a valid phone number")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


class NumberIn(BaseModel):
    e164: str = Field(min_length=3, max_length=32)
    #: Which carrier actually hosts this DID. Defaults to the deployment's primary rather
    #: than a hard-coded "bandwidth": a number recorded against a carrier that does not
    #: host it is unroutable, and the error surfaces far from the mistake.
    carrier: str | None = None
    #: "local" | "tollfree". Decides which registration regime gates the number.
    number_type: str = "local"


class AnsweredByOut(BaseModel):
    mode: str = "human"
    profile_id: uuid.UUID | None = None
    profile_name: str | None = None


class NumberOut(BaseModel):
    id: uuid.UUID
    e164: str
    carrier: str
    is_active: bool
    number_type: str = "local"
    status: str = "active"
    capabilities: dict = {}
    campaign_id: uuid.UUID | None = None
    #: "approved" | "pending" | "rejected" | "unknown". The console shows this so an
    #: operator learns a number cannot send BEFORE trying to send from it.
    registration: str = "unknown"
    registration_detail: str = ""
    #: P18: which P17 provider account bought this number, if any (NULL = env-configured
    #: carrier, or added by hand).
    provider_account_id: uuid.UUID | None = None
    provider_account_label: str | None = None
    #: The Inbox row's name for this number, or null when the number has no inbox row.
    inbox_name: str | None = None
    purchase_cost_cents: int | None = None
    monthly_cost_cents: int | None = None
    purchased_at: datetime | None = None
    #: Last provider order status/error (async orders are polled by the sweeper).
    order_detail: str | None = None
    #: P23b derived fields: "assistant" when this number is bound to an assistant flow.
    #: Who picks this number up. Derived from the bound flow, never stored: a one-node
    #: ASSISTANT flow means an assistant answers, anything else means a person does.
    answered_by: AnsweredByOut = AnsweredByOut()


@router.post("", response_model=NumberOut, status_code=201)
async def add_number(
    payload: NumberIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> NumberOut:
    """Minimal seed endpoint. P4 owns real search/order/port; here numbers are entered by
    hand so P1 can send from something."""
    normalized = to_e164(payload.e164)
    registry = getattr(request.app.state, "carriers", None)
    carrier_name = payload.carrier or (registry.primary_name if registry else "") or "bandwidth"

    # 4.5: a toll-free prefix always gates toll-free verification, regardless of a
    # manually supplied (possibly wrong) number_type - TFV vs 10DLC is not a matter of
    # operator say-so.
    number_type = "tollfree" if normalized[:5] in _TOLLFREE_PREFIXES else payload.number_type

    # 1.1: verify OWNERSHIP through the resolved carrier before accepting a manually
    # entered number - otherwise any org can claim ANY e164 (including one another
    # tenant already owns) and hijack its inbound traffic. Only asked when the resolved
    # carrier can actually verify (a NumberProvider - B6 made lookup_owned_number a
    # required Protocol member, so isinstance alone is enough now); a carrier that
    # is not a NumberProvider at all (or none configured) falls back to the pre-1.1
    # manual-add behaviour UNCHANGED - there is nothing more to check against.
    carrier_obj = registry.get(carrier_name) if registry else None
    # B1/4.21/E3: a caller-named carrier that cannot verify ownership - whether because
    # the registry cannot resolve the name at all, OR because the name resolves to a
    # real carrier that just isn't NumberProvider-capable (e.g. a voice-only adapter) -
    # must never silently skip the ownership gate below. Either shape lets ANY org
    # claim ANY e164 by naming a carrier OTHER than the deployment's real, verifiable
    # one. Only applies when (a) the carrier was actually named by the caller, and (b)
    # this deployment has SOME NumberProvider-capable carrier configured at all - a
    # deployment with none (e.g. a LiveKit-only deploy where `carrier` is only ever a
    # routing label, never a provisioning adapter) has nothing to verify against
    # regardless of the name given, so the pre-1.1 manual-add fallback still applies.
    has_verifiable_carrier = registry is not None and any(
        isinstance(registry.get(name), numbers_api.NumberProvider) for name in registry.names()
    )
    if (
        payload.carrier is not None
        and not isinstance(carrier_obj, numbers_api.NumberProvider)
        and has_verifiable_carrier
    ):
        if carrier_obj is None:
            raise ValidationFailedError(
                f"carrier {carrier_name!r} is not configured on this deployment"
            )
        raise ValidationFailedError(
            f"carrier {carrier_name!r} does not support number ownership verification "
            f"on this deployment"
        )
    if isinstance(carrier_obj, numbers_api.NumberProvider):
        settings = request.app.state.settings
        try:
            owned = await carrier_obj.lookup_owned_number(normalized)
        except Exception:
            owned = None
        if owned is False:
            raise ValidationFailedError("number is not owned by this provider account")
        if owned is None and not getattr(settings, "allow_unverified_number_add", False):
            raise ValidationFailedError(
                "cannot verify number ownership with this provider; enable "
                "allow_unverified_number_add to accept"
            )

    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        e164=normalized,
        carrier=carrier_name,
        number_type=number_type,
    )
    ctx.session.add(number)
    try:
        # P15: every number gets its Inbox in the SAME transaction it's created in - a
        # missing inbox row is treated as a bug elsewhere, never a lazy-create branch.
        # Flushed separately: Inbox.number_id is a plain FK column (no relationship()
        # between the two classes), so the unit of work has no dependency edge telling it
        # to insert org_numbers before inboxes - without this flush it can emit the Inbox
        # INSERT first and trip the foreign key.
        await ctx.session.flush()
        ctx.session.add(
            Inbox(id=uuid.uuid4(), org_id=ctx.org.id, name=normalized, number_id=number.id)
        )
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        # e164 is globally unique: a number belongs to exactly one org, ever.
        raise ConflictError(f"{normalized} is already registered") from exc
    return await _out(ctx.session, number)


@router.get("", response_model=list[NumberOut])
async def list_numbers(
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:read"))],
) -> list[NumberOut]:
    rows = list((await ctx.session.execute(sa.select(OrgNumber))).scalars().all())
    # P18: one query for every distinct provider_account_id in the page, not one
    # session.get() per row - a list endpoint over N numbers must not cost N extra
    # round trips just to label which P17 account bought each one.
    labels = await _provider_account_labels(
        ctx.session, {n.provider_account_id for n in rows if n.provider_account_id is not None}
    )
    inbox_names = await _inbox_names(ctx.session, {n.id for n in rows})

    # P23b: same batching convention as account labels and inbox names - fetch every
    # bound flow in ONE query, then pass the exact row into _out so list_numbers does
    # not become an N+1 on flow lookups.
    flow_ids = {n.call_flow_id for n in rows if n.call_flow_id is not None}
    flows_by_id: dict[uuid.UUID, CallFlow] = {}
    if flow_ids:
        flows_by_id = {
            f.id: f
            for f in (
                await ctx.session.execute(
                    sa.select(CallFlow).where(CallFlow.id.in_(flow_ids))
                )
            ).scalars().all()
        }

    # ... and every assistant name those flows point at, also in ONE query.
    assistant_ids: set[uuid.UUID] = set()
    for flow in flows_by_id.values():
        for raw_id in flows_svc.assistant_profile_ids(flow.definition):
            try:
                assistant_ids.add(uuid.UUID(raw_id))
            except (ValueError, TypeError):
                continue
    profile_names: dict[uuid.UUID, str] = {}
    if assistant_ids:
        profile_names = dict(
            (
                await ctx.session.execute(
                    sa.select(AgentProfile.id, AgentProfile.name).where(
                        AgentProfile.org_id == ctx.org.id,
                        AgentProfile.id.in_(assistant_ids),
                    )
                )
            ).all()
        )

    return [
        await _out(
            ctx.session,
            n,
            account_label=labels.get(n.provider_account_id) if n.provider_account_id else None,
            inbox_name=inbox_names.get(n.id),
            flow=flows_by_id.get(n.call_flow_id),
            profile_names=profile_names,
        )
        for n in rows
    ]


_TOLLFREE_PREFIXES = frozenset({"+1800", "+1833", "+1844", "+1855", "+1866", "+1877", "+1888"})

#: Sentinel distinguishing "_out's caller did not pass a label - look it up" from
#: "the caller looked it up already (possibly as None)". `None` itself is a valid,
#: meaningful value (no provider_account_id, or an account with a blank label).
_LABEL_UNSET = object()
_INBOX_UNSET = object()
_FLOW_UNSET = object()


async def _provider_account_labels(session, account_ids: set[uuid.UUID]) -> dict:
    if not account_ids:
        return {}
    rows = (
        await session.execute(
            sa.select(ProviderAccount.id, ProviderAccount.label).where(
                ProviderAccount.id.in_(account_ids)
            )
        )
    ).all()
    return {row.id: (row.label or None) for row in rows}


async def _inbox_names(session, number_ids: set[uuid.UUID]) -> dict:
    if not number_ids:
        return {}
    rows = (
        await session.execute(
            sa.select(Inbox.number_id, Inbox.name).where(Inbox.number_id.in_(number_ids))
        )
    ).all()
    return {row.number_id: row.name for row in rows}


async def _out(
    session,
    n: OrgNumber,
    *,
    account_label=_LABEL_UNSET,
    inbox_name=_INBOX_UNSET,
    flow=_FLOW_UNSET,
    profile_names: dict | None = None,
) -> NumberOut:
    state = await registration.registration_state(session, n)
    if account_label is _LABEL_UNSET:
        account_label = None
        if n.provider_account_id is not None:
            # Single-row callers (add_number/order/release/assign_campaign) only ever
            # build one NumberOut, so a per-call session.get() here is not the N+1 that
            # list_numbers() would have been - it already fetches its own labels above
            # and always passes account_label explicitly.
            account = await session.get(ProviderAccount, n.provider_account_id)
            if account is not None:
                account_label = account.label or None
    if inbox_name is _INBOX_UNSET:
        # Same reasoning as account_label: single-row callers may look this up here;
        # list_numbers() batches it with _inbox_names() and always passes the value.
        inbox_name = (
            await session.execute(
                sa.select(Inbox.name).where(Inbox.number_id == n.id)
            )
        ).scalar_one_or_none()
    if flow is _FLOW_UNSET:
        flow = await session.get(CallFlow, n.call_flow_id) if n.call_flow_id is not None else None

    answered_by = AnsweredByOut()
    assistant_profile_id: uuid.UUID | None = None
    if flow is not None:
        profile_ids = flows_svc.assistant_profile_ids(flow.definition)
        entry = flow.definition.get("entry")
        nodes = flow.definition.get("nodes")
        entry_node = nodes.get(entry) if isinstance(nodes, dict) else None
        if (
            profile_ids
            and isinstance(entry_node, dict)
            and entry_node.get("type") == "assistant"
        ):
            try:
                assistant_profile_id = uuid.UUID(profile_ids[0])
            except (ValueError, TypeError):
                assistant_profile_id = None
            if assistant_profile_id is not None:
                if profile_names is not None:
                    profile_name = profile_names.get(assistant_profile_id)
                else:
                    # Single-row callers only ever build one NumberOut; list_numbers
                    # batches this the same way it batches labels and inbox names.
                    profile_name = (
                        await session.execute(
                            sa.select(AgentProfile.name).where(
                                AgentProfile.org_id == n.org_id,
                                AgentProfile.id == assistant_profile_id,
                            )
                        )
                    ).scalar_one_or_none()
                answered_by = AnsweredByOut(
                    mode="assistant",
                    profile_id=assistant_profile_id,
                    profile_name=profile_name,
                )

    return NumberOut(
        id=n.id,
        e164=n.e164,
        carrier=n.carrier,
        is_active=n.is_active,
        number_type=n.number_type,
        status=n.status,
        capabilities=n.capabilities or {},
        campaign_id=n.campaign_id,
        registration=state.verdict,
        registration_detail=state.detail,
        provider_account_id=n.provider_account_id,
        provider_account_label=account_label,
        inbox_name=inbox_name,
        purchase_cost_cents=n.purchase_cost_cents,
        monthly_cost_cents=n.monthly_cost_cents,
        purchased_at=n.purchased_at,
        order_detail=n.order_detail,
        answered_by=answered_by,
    )


def _carrier_or_primary(request: Request, name: str | None):
    registry = getattr(request.app.state, "carriers", None)
    if registry is None or len(registry) == 0:
        raise CarrierNotConfiguredError("No carrier is configured on this deployment")
    if name:
        carrier = registry.get(name)
        if carrier is None:
            raise CarrierNotConfiguredError(f"Carrier {name!r} is not configured")
        return carrier
    primary = registry.primary()
    if primary is None:
        raise CarrierNotConfiguredError("No primary carrier")
    return primary


class SearchOut(BaseModel):
    e164: str
    number_type: str
    region: str
    locality: str
    monthly_cost: str
    setup_cost: str = ""
    capabilities: dict
    monthly_cost_cents: int | None = None
    setup_cost_cents: int | None = None


@router.get("/available", response_model=list[SearchOut])
async def search_available(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
    carrier: str | None = None,
    area_code: str = "",
    contains: str = "",
    locality: str = "",
    region: str = "",
    number_type: str = "local",
    limit: int = 20,
) -> list[SearchOut]:
    """Numbers we could order. Asks ONE carrier - whichever is named, or the primary.

    Deliberately not a fan-out: the same number is not orderable from two carriers, and
    merging results would hide which carrier a number would come from - which is exactly
    what decides its registration regime and its routing.
    """
    provider = numbers_api.as_provider(_carrier_or_primary(request, carrier))
    found = await provider.search_numbers(
        numbers_api.NumberSearch(
            area_code=area_code,
            contains=contains,
            locality=locality,
            region=region,
            number_type=number_type,
            limit=limit,
        )
    )
    return [
        SearchOut(
            e164=n.e164,
            number_type=n.number_type,
            region=n.region,
            locality=n.locality,
            monthly_cost=n.monthly_cost,
            setup_cost=n.setup_cost,
            capabilities=n.capabilities,
            monthly_cost_cents=n.monthly_cost_cents,
            setup_cost_cents=n.setup_cost_cents,
        )
        for n in found
    ]


class OrderIn(BaseModel):
    e164: str = Field(min_length=3, max_length=32)
    carrier: str | None = None
    campaign_id: uuid.UUID | None = None
    #: P18: the cost row the client selected from GET /numbers/available (a search
    #: result's monthly_cost_cents/setup_cost_cents). Only a fallback - whatever the
    #: carrier itself reports on the order response always wins, since the search-time
    #: quote can be stale by the time the order lands. Bounded (0..$1,000,000.00) so a
    #: malformed/hostile client payload can never land as an absurd or negative cost.
    monthly_cost_cents: int | None = Field(default=None, ge=0, le=100_000_000)
    setup_cost_cents: int | None = Field(default=None, ge=0, le=100_000_000)


@router.post("/order", response_model=NumberOut, status_code=201)
async def order(
    payload: OrderIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> NumberOut:
    carrier_obj = _carrier_or_primary(request, payload.carrier)
    provider = numbers_api.as_provider(carrier_obj)
    normalized = to_e164(payload.e164)

    # 4.8: pre-check BEFORE ordering so an already-registered number never reaches the
    # carrier at all - the common case that used to only surface as an orphaned purchase
    # after the IntegrityError below.
    existing = (
        await ctx.session.execute(sa.select(OrgNumber.id).where(OrgNumber.e164 == normalized))
    ).first()
    if existing is not None:
        raise ConflictError(f"{normalized} is already registered")

    # Prepaid hard gate: never buy a number the balance cannot pay the first month of.
    await telephony_billing.require_number_credit(ctx.session, ctx.org.id, carrier_obj.name)
    result = await provider.order_number(normalized)

    # P18: only attribute the purchase to a provider_accounts row when THIS carrier
    # object actually came from that org's active P17 DB account - not merely because
    # one happens to exist. Two different orgs (or an org with both an env carrier and
    # an unrelated DB account for the same provider name) must never have their numbers
    # cross-linked. registry_org.db_backed_providers() answers exactly that, from the
    # same cache CarrierRegistryProxy itself resolved `carrier_obj` from.
    provider_account_id: uuid.UUID | None = None
    if carrier_obj.name in registry_org.db_backed_providers(ctx.org.id):
        account = await provider_accounts_svc.active_account_for(ctx.session, carrier_obj.name)
        if account is not None:
            provider_account_id = account.id

    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        e164=result.e164,
        carrier=carrier_obj.name,
        provider_ref=result.provider_ref or None,
        # Whatever the carrier SAID. Recording a pending order as active means inbound is
        # dropped with no trace until somebody thinks to ask why - EXCEPT when this
        # carrier has no order_status to ever resolve a non-active result later: for
        # those (pre-P18 behaviour), a pending/unknown status still starts routable
        # rather than being permanently stranded with no polling path to fix it.
        status=result.status,
        is_active=result.status == "active" or not hasattr(carrier_obj, "order_status"),
        capabilities=result.capabilities or {},
        number_type="tollfree" if result.e164[:5] in _TOLLFREE_PREFIXES else "local",
        campaign_id=payload.campaign_id,
        provider_account_id=provider_account_id,
        # The carrier's own reported cost always wins; the client's search-time
        # selection (payload.*_cost_cents) is only a fallback for a carrier that
        # doesn't echo cost on the order response at all.
        purchase_cost_cents=(
            result.setup_cost_cents
            if result.setup_cost_cents is not None
            else payload.setup_cost_cents
        ),
        monthly_cost_cents=(
            result.monthly_cost_cents
            if result.monthly_cost_cents is not None
            else payload.monthly_cost_cents
        ),
        purchased_at=datetime.now(timezone.utc),
        order_detail=result.status if result.status != "active" else None,
    )
    ctx.session.add(number)
    try:
        # P15: every number gets its Inbox in the SAME transaction it's created in - see
        # the matching comment in add_number() for why the flush must come first.
        await ctx.session.flush()
        ctx.session.add(
            Inbox(id=uuid.uuid4(), org_id=ctx.org.id, name=result.e164, number_id=number.id)
        )
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        # 4.8: the number was purchased at the carrier (a race lost the pre-check above)
        # but could not be stored - release it there so it is not orphaned and billed
        # forever. The conflict still wins the response either way.
        try:
            await carrier_obj.release_number(result.e164, result.provider_ref)
        except Exception:
            pass
        raise ConflictError(f"{result.e164} is already registered") from exc
    await telephony_billing.charge_new_number(ctx.session, ctx.org.id, number)
    await ctx.session.commit()
    return await _out(ctx.session, number)


@router.delete("/{number_id}", response_model=NumberOut)
async def release(
    number_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> NumberOut:
    """Release at the carrier, mark released here - but KEEP THE ROW (phase-4-plan DR-5).

    Threads, messages and above all the consent ledger reference this number, and the
    ledger is the evidence that somebody opted out. Deleting the row would destroy the
    record we would need to prove we honoured a STOP.
    """
    number = await ctx.session.get(OrgNumber, number_id)
    if number is None:
        raise NotFoundError("Number not found")
    if number.status == "released":
        return await _out(ctx.session, number)

    registry = getattr(request.app.state, "carriers", None)
    carrier_obj = registry.get(number.carrier) if registry else None
    if carrier_obj is not None and isinstance(carrier_obj, numbers_api.NumberProvider):
        await carrier_obj.release_number(number.e164, number.provider_ref)

    number.status = "released"
    number.is_active = False
    number.released_at = datetime.now(timezone.utc)
    await ctx.session.commit()
    return await _out(ctx.session, number)


class AssignIn(BaseModel):
    campaign_id: uuid.UUID | None = None


@router.patch("/{number_id}/campaign", response_model=NumberOut)
async def assign_campaign(
    number_id: uuid.UUID,
    payload: AssignIn,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> NumberOut:
    number = await ctx.session.get(OrgNumber, number_id)
    if number is None:
        raise NotFoundError("Number not found")
    if number.number_type == "tollfree" and payload.campaign_id is not None:
        raise ValidationFailedError(
            f"{number.e164} is toll-free; it is gated by toll-free verification, not by a "
            f"10DLC campaign. Assigning one would not change its ability to send."
        )
    if payload.campaign_id is not None:
        campaign = await ctx.session.get(Campaign, payload.campaign_id)
        if campaign is None:
            raise NotFoundError("Campaign not found")
    number.campaign_id = payload.campaign_id
    await ctx.session.commit()
    return await _out(ctx.session, number)


class AnsweredByIn(BaseModel):
    mode: str
    profile_id: uuid.UUID | None = None


@router.patch("/{number_id}/answered-by", response_model=NumberOut)
async def set_answered_by(
    number_id: uuid.UUID,
    payload: AnsweredByIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> NumberOut:
    number = await ctx.session.get(OrgNumber, number_id)
    if number is None or number.org_id != ctx.org.id:
        raise NotFoundError("Number not found")

    if payload.mode not in ("human", "assistant"):
        raise ValidationFailedError("Choose who answers this number: a person or an assistant.")

    profile_id: uuid.UUID | None = None
    if payload.mode == "assistant":
        if payload.profile_id is None:
            raise ValidationFailedError("Pick which assistant should answer.")
        profile = await agent_svc.get_profile(ctx.session, ctx.org.id, payload.profile_id)
        readiness = await agent_svc.go_live_readiness(
            ctx.session, request.app.state.settings, org=ctx.org, profile=profile
        )
        if not readiness["ready"]:
            raise ValidationFailedError(
                f"{profile.name} is not ready to answer calls yet. Finish setting it up first."
            )

        definition = {
            "entry": "assistant",
            "nodes": {
                "assistant": {
                    "type": "assistant",
                    "profile_id": str(profile.id),
                }
            },
        }
        flow = await flows_svc.upsert_single_node_flow(
            ctx.session,
            ctx.org.id,
            name=f"Answered by an assistant ({number.e164})",
            definition=definition,
        )
        await flows_svc.bind_number(ctx.session, ctx.org.id, number.id, flow.id)
        profile_id = profile.id
    else:
        # Restore the seeded default ring flow. A NULL binding is the pre-P12 "ring
        # normally" behaviour and is still valid when the org has no active Default row.
        default_flow_id = (
            await ctx.session.execute(
                sa.select(CallFlow.id).where(
                    CallFlow.org_id == ctx.org.id,
                    CallFlow.name == "Default",
                    CallFlow.status == "active",
                )
            )
        ).scalar_one_or_none()
        await flows_svc.bind_number(ctx.session, ctx.org.id, number.id, default_flow_id)

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        action="number.answered_by",
        target_type="org_number",
        target_id=str(number.id),
        detail={
            "e164": number.e164,
            "mode": payload.mode,
            "profile_id": str(profile_id) if profile_id is not None else None,
        },
    )
    await ctx.session.commit()
    return await _out(ctx.session, number)


class NumberReputationOut(BaseModel):
    """P14 DR-7: derived, trailing-7-day per-number stats. No third-party reputation API -
    every field here is computed from our own `messages` rows."""

    e164: str
    carrier: str
    window_start: datetime
    window_end: datetime
    volume: int
    delivered: int
    failed: int
    rejected: int
    delivery_rate: float | None
    carrier_error_rate: float | None
    spam_class_error_count: int


@router.get("/reputation", response_model=list[NumberReputationOut])
async def number_reputation(
    ctx: Annotated[OrgContext, Depends(require_permission("reports:read"))],
) -> list[NumberReputationOut]:
    stats = await reputation_svc.compute_number_stats(ctx.session, ctx.org.id)
    await ctx.session.commit()
    return [
        NumberReputationOut(
            e164=s.e164,
            carrier=s.carrier,
            window_start=s.window_start,
            window_end=s.window_end,
            volume=s.volume,
            delivered=s.delivered,
            failed=s.failed,
            rejected=s.rejected,
            delivery_rate=s.delivery_rate,
            carrier_error_rate=s.carrier_error_rate,
            spam_class_error_count=s.spam_class_error_count,
        )
        for s in stats
    ]
