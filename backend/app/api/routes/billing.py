"""Customer billing surface: prepaid credit balance, ledger, usage, top-ups, rates, and
saved payment methods.

Cost never appears in this module's responses. Money in responses is integer micros in
``*_micros`` fields; prose messages never mention micros or ledger entries.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.auth.deps import (
    OrgContext,
    check_org_selfie_step_up,
    require_owner,
    require_permission,
)
from app.errors import (
    FeatureUnavailableError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.models import Call, CreditLedgerEntry, PaymentMethod, Plan
from app.services import ai_usage, credits, stripe_client
from app.services import bundles as bundles_svc
from app.services import audit as audit_svc
from app.services import plans as plans_svc
from app.services import spend as spend_svc

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


class NumberCheckoutIn(BaseModel):
    numbers: list[str] = Field(min_length=1, max_length=20)


@router.get("/number-purchases/current")
async def current_number_purchase(
    ctx: Annotated[OrgContext, Depends(require_owner)],
) -> dict | None:
    from app.models import NumberPurchase
    from app.services import number_purchases

    row = (
        await ctx.session.execute(
            sa.select(NumberPurchase)
            .where(
                NumberPurchase.org_id == ctx.org.id,
                NumberPurchase.state.in_(
                    ("checkout", "paid", "provisioning", "activating", "needs_attention")
                ),
            )
            .order_by(NumberPurchase.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return number_purchases.public(row) if row else None


@router.post("/number-purchases/{purchase_id}/cancel")
async def cancel_number_checkout(
    purchase_id: uuid.UUID, request: Request, ctx: Annotated[OrgContext, Depends(require_owner)]
) -> dict:
    from app.models import NumberPurchase

    row = (
        await ctx.session.execute(
            sa.select(NumberPurchase)
            .where(
                NumberPurchase.id == purchase_id,
                NumberPurchase.org_id == ctx.org.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Purchase not found")
    if row.state != "checkout" or not row.checkout_id:
        raise ValidationFailedError("This purchase cannot be cancelled here")
    stripe = stripe_client._stripe(request.app.state.settings)
    remote = await stripe_client._run_sync(stripe.checkout.Session.retrieve, row.checkout_id)
    if remote.get("status") == "complete":
        raise ValidationFailedError(
            "Payment has already completed. Continue setting up your numbers."
        )
    if remote.get("status") != "expired":
        await stripe_client._run_sync(stripe.checkout.Session.expire, row.checkout_id)
    row.state = "expired"
    await ctx.session.commit()
    return {"cancelled": True}


@router.post("/number-checkout")
async def number_checkout(
    payload: NumberCheckoutIn, request: Request, ctx: Annotated[OrgContext, Depends(require_owner)]
) -> dict:
    from app.services import number_purchases

    purchase = await number_purchases.create(
        ctx.session, request.app.state.settings, ctx.org.id, payload.numbers
    )
    return number_purchases.public(purchase)


@router.post("/number-purchases/{purchase_id}/complete")
async def complete_number_purchase(
    purchase_id: uuid.UUID, request: Request, ctx: Annotated[OrgContext, Depends(require_owner)]
) -> dict:
    from app.models import NumberPurchase
    from app.services import number_purchases

    purchase = await ctx.session.get(NumberPurchase, purchase_id)
    if purchase is None or purchase.org_id != ctx.org.id:
        raise NotFoundError("Purchase not found")
    return number_purchases.public(await number_purchases.fulfill(ctx.session, request, purchase))


def _actor(ctx: OrgContext) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    return ctx.actor_user_id, (ctx.api_key.id if ctx.api_key is not None else None)


def _checkout_urls(settings) -> tuple[str, str]:
    """Return Stripe Checkout return URLs, derived from public_web_url when unset."""
    base = (getattr(settings, "public_web_url", "") or "").rstrip("/")
    success_url = getattr(settings, "stripe_success_url", "") or ""
    cancel_url = getattr(settings, "stripe_cancel_url", "") or ""

    if not success_url and base:
        success_url = f"{base}/settings/billing?topup=done"
    if not cancel_url and base:
        cancel_url = f"{base}/settings/billing?topup=cancelled"

    return success_url, cancel_url


class TopupIn(BaseModel):
    amount_micros: int


class SubscriptionCheckoutIn(BaseModel):
    plan_code: str


class AutoRechargeIn(BaseModel):
    enabled: bool
    threshold_micros: int | None = None
    amount_micros: int | None = None
    payment_method_id: uuid.UUID | None = None


class PaymentMethodIn(BaseModel):
    stripe_payment_method_id: str
    # Accepted for wire compatibility with older clients and then IGNORED: customer
    # identity is derived from this org's stored payment-method rows (or freshly ensured
    # from the org), never from the request body.
    customer_id: str | None = None


AI_METRIC_KIND = {
    "ai_voice_seconds": "voice",
    "stt_seconds": "stt",
    "tts_characters": "tts",
    "llm_tokens_in": "llm",
    "llm_tokens_out": "llm",
}


def _usage_range(
    start: date | None,
    end: date | None,
) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    if start is None:
        start_dt = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    else:
        start_dt = datetime.combine(start, time.min, tzinfo=timezone.utc)
    if end is None:
        end_dt = now
    else:
        end_dt = datetime.combine(end, time.max, tzinfo=timezone.utc)
    if start_dt > end_dt:
        raise ValidationFailedError("from must be on or before to")
    if end_dt - start_dt > timedelta(days=366):
        raise ValidationFailedError("Date range must not exceed 366 days")
    return start_dt, end_dt


# OWNER-ONLY, deliberately: a non-owner member calling this gets 403 ``owner_only``. This
# route is polled app-wide by the console, so the refusal has to stay a well-formed 403
# carrying that one STABLE code - never a 401, never a 500, and never a bare "forbidden"
# that the console cannot distinguish from a dead session.
def _summary_warning(org, balance: int, last_topup) -> str | None:  # noqa: ANN001
    """Billing v2: a prepaid org warns below max($5, a day's spend) and is 'empty' at $0;
    others keep the old fraction-of-last-top-up rule."""
    if org.telephony_prepaid:
        from app.services import billing_alerts

        state = billing_alerts.state_for(balance, int(org.warn_threshold_micros or 0))
        return {"exhausted": "empty", "low": "low"}.get(state)
    return credits.warning_level(
        balance, int(last_topup.amount_micros) if last_topup is not None else 0
    )


@router.get("/summary")
async def get_summary(
    ctx: Annotated[OrgContext, Depends(require_owner)],
) -> dict:
    balance = await credits.balance(ctx.session, ctx.org.id)
    reserved = await credits.outstanding_reserves(ctx.session, ctx.org.id)
    available = max(balance - reserved, 0)

    last_topup = (
        await ctx.session.execute(
            sa.select(CreditLedgerEntry)
            .where(
                CreditLedgerEntry.org_id == ctx.org.id,
                CreditLedgerEntry.entry_type == "topup",
            )
            .order_by(CreditLedgerEntry.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    last_topup_dict = None
    if last_topup is not None:
        last_topup_dict = {
            "amount_micros": int(last_topup.amount_micros),
            "at": last_topup.created_at.isoformat(),
        }

    return {
        "balance_micros": balance,
        "reserved_micros": reserved,
        "available_micros": available,
        "warning": _summary_warning(ctx.org, balance, last_topup),
        "auto_recharge": ctx.org.credit_auto_recharge,
        # True when texting/calling draw from this balance and stop when it is empty.
        "telephony_prepaid": bool(ctx.org.telephony_prepaid),
        "last_topup": last_topup_dict,
        "fallback": ai_usage.credit_fallback(ctx.org),
        "bundles": {
            kind: await bundles_svc.units(ctx.session, ctx.org.id, kind) for kind in ("sms", "mms")
        },
        "billing_state": ctx.org.billing_state,
        "warn_threshold_micros": int(ctx.org.warn_threshold_micros or 0),
        "avg_daily_spend_micros": int(ctx.org.avg_daily_spend_micros or 0),
    }


@router.get("/ledger")
async def get_ledger(
    ctx: Annotated[OrgContext, Depends(require_owner)],
    limit: int = Query(50, ge=1),
    cursor: str | None = None,
) -> dict:
    limit = min(limit, 200)

    stmt = sa.select(CreditLedgerEntry).where(CreditLedgerEntry.org_id == ctx.org.id)

    if cursor:
        created_str, _, id_str = cursor.partition("|")
        try:
            created_at = datetime.fromisoformat(created_str)
            cursor_id = uuid.UUID(id_str)
        except ValueError as exc:
            raise ValidationFailedError("Invalid cursor") from exc
        stmt = stmt.where(
            sa.or_(
                CreditLedgerEntry.created_at < created_at,
                sa.and_(
                    CreditLedgerEntry.created_at == created_at,
                    CreditLedgerEntry.id < cursor_id,
                ),
            )
        )

    stmt = stmt.order_by(
        CreditLedgerEntry.created_at.desc(),
        CreditLedgerEntry.id.desc(),
    ).limit(limit + 1)

    rows = list((await ctx.session.execute(stmt)).scalars().all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = f"{last.created_at.isoformat()}|{last.id}"

    return {
        "entries": [
            {
                "id": str(row.id),
                "entry_type": row.entry_type,
                "amount_micros": int(row.amount_micros),
                "balance_after_micros": int(row.balance_after_micros),
                "reference": row.reference,
                "note": row.note,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
        "next_cursor": next_cursor,
    }


@router.get("/usage")
async def get_usage(
    ctx: Annotated[OrgContext, Depends(require_owner)],
    start_date: Annotated[date | None, Query(alias="from")] = None,
    end_date: Annotated[date | None, Query(alias="to")] = None,
) -> dict:
    start, end = _usage_range(start_date, end_date)
    summary = await ai_usage.usage_summary(
        ctx.session,
        ctx.org.id,
        start=start,
        end=end,
    )
    total = sum(int(item.get("price_micros", 0)) for item in summary)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "by_metric": summary,
        "total_price_micros": total,
    }


@router.get("/usage/calls/{call_id}")
async def get_usage_call(
    call_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_owner)],
) -> dict:
    call = (
        await ctx.session.execute(
            sa.select(Call).where(
                Call.org_id == ctx.org.id,
                Call.id == call_id,
            )
        )
    ).scalar_one_or_none()
    if call is None:
        raise NotFoundError("Call not found")

    return await ai_usage.usage_for_call(ctx.session, ctx.org.id, call_id)


@router.post("/topups")
async def create_topup(
    payload: TopupIn,
    ctx: Annotated[OrgContext, Depends(require_permission("org:billing"))],
    request: Request,
) -> dict:
    amount = payload.amount_micros
    exact_amounts = {25_000_000, 50_000_000, 100_000_000}
    if amount not in exact_amounts and not (5_000_000 <= amount <= 5_000_000_000):
        raise ValidationFailedError(
            "Amount must be $25, $50, $100, or a custom amount between $5 and $5,000."
        )

    settings = request.app.state.settings
    success_url, cancel_url = _checkout_urls(settings)
    checkout = await stripe_client.create_checkout_session(
        settings,
        org=ctx.org,
        amount_micros=amount,
        success_url=success_url,
        cancel_url=cancel_url,
    )

    actor_user, actor_key = _actor(ctx)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="billing.topup_started",
        target_type="org",
        target_id=str(ctx.org.id),
        actor_user_id=actor_user,
        actor_api_key_id=actor_key,
        detail={"amount_micros": amount},
    )
    await ctx.session.commit()

    return {"checkout_url": checkout["url"]}


class BundleCheckoutIn(BaseModel):
    kind: str = Field(pattern="^(sms|mms)$")
    qty: int = Field(ge=1, le=500)


@router.get("/bundles")
async def get_bundles(
    ctx: Annotated[OrgContext, Depends(require_permission("org:billing"))],
) -> dict:
    """Units left, and the price list with the volume rule, for the Bundles card."""
    from app.services import bundles as bundles_svc

    out: dict = {"volume_min_qty": bundles_svc.VOLUME_MIN_QTY,
                 "volume_discount_bps": bundles_svc.VOLUME_DISCOUNT_BPS, "kinds": {}}
    for kind in ("sms", "mms"):
        out["kinds"][kind] = {
            "units": await bundles_svc.units(ctx.session, ctx.org.id, kind),
            "units_per_bundle": bundles_svc.UNITS_PER_BUNDLE[kind],
            "list_micros": await bundles_svc.bundle_list_price(ctx.session, kind),
            "volume_discount": kind in bundles_svc.VOLUME_DISCOUNT_KINDS,
            "pay_as_you_go_micros": await telephony_billing_price(ctx, f"{kind}_out"),
        }
    return out


async def telephony_billing_price(ctx: OrgContext, metric: str) -> int:
    from app.services import telephony_billing

    # unit_price honours a per-org rate override; the carrier does not change flat prices.
    return await telephony_billing.unit_price(ctx.session, ctx.org.id, "telnyx", metric)


@router.post("/bundles/checkout")
async def create_bundle_checkout(
    payload: BundleCheckoutIn,
    ctx: Annotated[OrgContext, Depends(require_permission("org:billing"))],
    request: Request,
) -> dict:
    from app.services import bundles as bundles_svc
    from app.services import payments as payments_svc

    settings = request.app.state.settings
    success_url, cancel_url = _checkout_urls(settings)
    success_url = success_url.replace("topup=done", "bundle=done")
    row, q = await payments_svc.start_bundle_payment(
        ctx.session, ctx.org.id, kind=payload.kind, qty=payload.qty
    )
    size = bundles_svc.UNITS_PER_BUNDLE[payload.kind]
    name = f"{size:,} {payload.kind.upper()} bundle"
    if q["discount"] > 0:
        name += f" ({bundles_svc.VOLUME_DISCOUNT_BPS // 100}% volume discount)"
    checkout = await stripe_client.create_bundle_checkout_session(
        settings,
        org=ctx.org,
        kind=payload.kind,
        qty=payload.qty,
        unit_amount_micros=q["unit_paid"],
        product_name=name,
        payment_id=str(row.id),
        success_url=success_url,
        cancel_url=cancel_url,
    )
    row.stripe_checkout_id = checkout["id"]
    actor_user, actor_key = _actor(ctx)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="billing.bundle_checkout_started",
        target_type="org",
        target_id=str(ctx.org.id),
        actor_user_id=actor_user,
        actor_api_key_id=actor_key,
        detail={"kind": payload.kind, "qty": payload.qty, "paid_micros": q["paid"]},
    )
    await ctx.session.commit()
    return {
        "checkout_url": checkout["url"],
        "list_micros": q["list"],
        "discount_micros": q["discount"],
        "paid_micros": q["paid"],
        "units": q["units"],
    }


@router.post("/subscription/checkout")
async def create_subscription_checkout(
    payload: SubscriptionCheckoutIn,
    ctx: Annotated[OrgContext, Depends(require_permission("org:billing"))],
    request: Request,
) -> dict:
    """Start Stripe Checkout for a monthly plan. Same owner permission as a top-up."""
    code = (payload.plan_code or "").strip()
    plan = await ctx.session.get(Plan, code) if code else None
    if plan is None or not plan.is_active:
        raise NotFoundError("Plan not found")

    # THE MONEY GUARD. A plan whose Stripe price id is not configured cannot be bought,
    # and the refusal names the plan so the operator knows exactly which one to fix. The
    # alternative - falling back to some locally computed amount - would charge a real
    # card an amount nobody at Stripe ever agreed to.
    if not (plan.stripe_price_id or "").strip():
        raise FeatureUnavailableError(
            f"Plan {plan.name} ({plan.code}) is not available for checkout yet - "
            "its Stripe price id is not configured."
        )

    settings = request.app.state.settings
    success_url, cancel_url = _checkout_urls(settings)
    checkout = await stripe_client.create_subscription_checkout_session(
        settings,
        org=ctx.org,
        price_id=plan.stripe_price_id.strip(),
        plan_code=plan.code,
        success_url=success_url,
        cancel_url=cancel_url,
    )

    actor_user, actor_key = _actor(ctx)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="billing.subscription_started",
        target_type="org",
        target_id=str(ctx.org.id),
        actor_user_id=actor_user,
        actor_api_key_id=actor_key,
        detail={"plan_code": plan.code},
    )
    await ctx.session.commit()

    return {"checkout_url": checkout["url"]}


@router.patch("/auto-recharge")
async def patch_auto_recharge(
    payload: AutoRechargeIn,
    ctx: Annotated[OrgContext, Depends(require_permission("org:billing"))],
) -> dict | None:
    if payload.enabled:
        if (
            payload.threshold_micros is None
            or payload.amount_micros is None
            or payload.payment_method_id is None
        ):
            raise ValidationFailedError(
                "Auto-recharge needs a threshold, amount, and payment method."
            )
        if payload.threshold_micros <= 0 or payload.amount_micros <= 0:
            raise ValidationFailedError("Threshold and amount must be positive.")
        if payload.amount_micros < 10_000_000 or payload.amount_micros % 10_000_000:
            raise ValidationFailedError("Auto-recharge amount must be a multiple of $10.")

        pm = (
            await ctx.session.execute(
                sa.select(PaymentMethod).where(
                    PaymentMethod.org_id == ctx.org.id,
                    PaymentMethod.id == payload.payment_method_id,
                )
            )
        ).scalar_one_or_none()
        if pm is None:
            raise NotFoundError("Payment method not found")

        # Preserve any other keys already in the JSON; the assistant fallback
        # lives in this same dict.
        current = dict(ctx.org.credit_auto_recharge or {})
        current.update(
            {
                "enabled": True,
                "threshold_micros": payload.threshold_micros,
                "amount_micros": payload.amount_micros,
                "payment_method_id": str(payload.payment_method_id),
            }
        )
        # Re-enabling (e.g. after a new card) starts the decline count over.
        for key in ("last_failure_at", "last_failure", "disabled_reason"):
            current.pop(key, None)
        ctx.org.auto_recharge_failures = 0
        ctx.org.credit_auto_recharge = current
    else:
        # Keep the dict drops only the recharge keys. The assistant fallback key
        # lives in the same JSON, so setting the whole column to None would
        # silently discard it.
        current = dict(ctx.org.credit_auto_recharge or {})
        for key in (
            "enabled",
            "threshold_micros",
            "amount_micros",
            "payment_method_id",
        ):
            current.pop(key, None)
        current["enabled"] = False

        # Only store None when the result is exactly {"enabled": False} and
        # there were no other non-recharge keys to preserve.
        if set(current.keys()) == {"enabled"}:
            ctx.org.credit_auto_recharge = None
        else:
            ctx.org.credit_auto_recharge = current

    actor_user, actor_key = _actor(ctx)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="billing.auto_recharge_updated",
        target_type="org",
        target_id=str(ctx.org.id),
        actor_user_id=actor_user,
        actor_api_key_id=actor_key,
        detail={
            "enabled": payload.enabled,
            "credit_auto_recharge": ctx.org.credit_auto_recharge,
        },
    )
    await ctx.session.commit()
    return ctx.org.credit_auto_recharge


def _plan_sort_key(plan: Plan) -> tuple[int, int, str]:
    """Catalogue order: the seeded tiers first, in SAMPLE_PLAN_CODES order (starter,
    standard, professional - the meaningful ladder, which is neither alphabetical nor
    insertion order), then any operator-added plan after them, cheapest first and code
    as the final tiebreak so the list is fully deterministic."""
    try:
        rank = plans_svc.SAMPLE_PLAN_CODES.index(plan.code)
    except ValueError:
        rank = len(plans_svc.SAMPLE_PLAN_CODES)
    return (rank, int(plan.monthly_price_micros or 0), plan.code)


@router.get("/plans")
async def list_plans(
    ctx: Annotated[OrgContext, Depends(require_owner)],
) -> list[dict]:
    """The plan catalogue the picker renders. Read-only, no org data, no cost.

    Owner-only, because the catalogue only ever feeds the billing console. It is NOT
    identity gated (``org:billing``) - looking at a price list must never make someone
    pass an ID check - so ``require_owner`` is the right lock rather than the spending
    permission.

    Inactive plans ARE returned, with ``is_active`` telling the truth: the client
    renders them as not purchasable rather than hiding them, so a customer on a
    retired plan still sees the plan they are on instead of a gap.

    ``stripe_price_id`` is passed through verbatim, INCLUDING NULL. It is how the
    client decides a plan can be bought at all; a plan without one must still appear
    and be shown as not purchasable. Dropping those rows - every seeded plan today -
    would render an empty picker that looked perfectly healthy.
    """
    # `plans` is platform-wide, not tenant-scoped, so no org filter applies here.
    rows = (await ctx.session.execute(sa.select(Plan))).scalars().all()

    return [
        {
            "code": plan.code,
            "name": plan.name,
            "included": dict(plan.included or {}),
            "overage_rates": dict(plan.overage_rates or {}),
            "monthly_price_micros": int(plan.monthly_price_micros or 0),
            "stripe_price_id": plan.stripe_price_id,
            "is_active": bool(plan.is_active),
        }
        for plan in sorted(rows, key=_plan_sort_key)
    ]


@router.get("/rates")
async def get_rates(
    ctx: Annotated[OrgContext, Depends(require_owner)],
) -> list[dict]:
    # AI providers are present in DEFAULT_RATES_MICROS, but their metrics are scope
    # 'ai'. This customer rate sheet exposes only the AI metrics with customer prices.
    providers = [
        provider
        for provider, rates in spend_svc.DEFAULT_RATES_MICROS.items()
        if any(metric in AI_METRIC_KIND for metric in rates)
    ]

    out: list[dict] = []
    for provider in providers:
        for metric, kind in AI_METRIC_KIND.items():
            unit_cost, _is_override, known = await spend_svc.resolve_rate(
                ctx.session,
                provider,
                metric,
            )
            if not known:
                continue
            price = credits.price_for(
                cost_micros=unit_cost,
                kind=kind,
                quantity=1,
                org=ctx.org,
            )
            out.append(
                {
                    "provider": provider,
                    "metric": metric,
                    "price_micros": price,
                    "currency": "USD",
                }
            )
    return out


@router.get("/payment-methods")
async def list_payment_methods(
    ctx: Annotated[OrgContext, Depends(require_owner)],
) -> list[dict]:
    rows = (
        (
            await ctx.session.execute(
                sa.select(PaymentMethod)
                .where(PaymentMethod.org_id == ctx.org.id)
                .order_by(PaymentMethod.is_default.desc(), PaymentMethod.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    return [
        {
            "id": str(row.id),
            "brand": row.brand,
            "last4": row.last4,
            "is_default": row.is_default,
        }
        for row in rows
    ]


@router.post("/payment-methods", status_code=201)
async def add_payment_method(
    payload: PaymentMethodIn,
    ctx: Annotated[OrgContext, Depends(require_permission("org:billing"))],
    request: Request,
) -> dict:
    settings = request.app.state.settings
    # P41: changing how the business pays is a classic account-takeover move.
    await check_org_selfie_step_up(request, ctx, action="payment_method_change")

    # The org has no stripe_customer_id column (Fable owns the schema), so the
    # customer id is carried on payment_method rows and re-used from there. The
    # caller-supplied ``payload.customer_id`` is deliberately NOT trusted: honouring
    # it let any ``org:billing`` member point the attach at an arbitrary (foreign)
    # customer. The field stays in the request schema for wire compatibility but is
    # ignored; identity comes from this org's own rows, or from a customer ensured
    # against the org object itself.
    existing_rows = (
        (
            await ctx.session.execute(
                sa.select(PaymentMethod)
                .where(PaymentMethod.org_id == ctx.org.id)
                .order_by(PaymentMethod.created_at.desc(), PaymentMethod.id.desc())
            )
        )
        .scalars()
        .all()
    )
    existing_customer_id = existing_rows[0].stripe_customer_id if existing_rows else None
    customer_id = await stripe_client.ensure_customer(
        settings,
        org=ctx.org,
        existing_customer_id=existing_customer_id,
    )

    attached = await stripe_client.attach_payment_method(
        settings,
        payment_method_id=payload.stripe_payment_method_id,
        customer_id=customer_id,
    )

    is_default = not existing_rows

    # P41: a card that belongs to a banned business is refused and detached again.
    fingerprint = attached.get("fingerprint")
    if fingerprint:
        from app.services import ban_list

        hit = await ban_list.matches(
            ctx.session, [ban_list.identifier("card_fingerprint", fingerprint)]
        )
        if hit:
            try:
                await stripe_client.detach_payment_method(
                    settings, payment_method_id=attached["id"]
                )
            except Exception:  # noqa: BLE001 - refusing the card matters more
                pass
            raise PermissionDeniedError(
                "This card cannot be used. Contact support.", code="payment_method_refused"
            )

    pm = PaymentMethod(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        stripe_customer_id=customer_id,
        stripe_payment_method_id=attached["id"],
        brand=attached.get("brand", ""),
        last4=attached.get("last4", ""),
        is_default=is_default,
        card_fingerprint=fingerprint,
    )
    ctx.session.add(pm)

    actor_user, actor_key = _actor(ctx)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="billing.payment_method_added",
        target_type="payment_method",
        target_id=str(pm.id),
        actor_user_id=actor_user,
        actor_api_key_id=actor_key,
        detail={"brand": pm.brand, "last4": pm.last4},
    )
    await ctx.session.commit()

    return {
        "id": str(pm.id),
        "brand": pm.brand,
        "last4": pm.last4,
        "is_default": pm.is_default,
    }


@router.delete("/payment-methods/{payment_method_id}", status_code=204)
async def remove_payment_method(
    payment_method_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("org:billing"))],
    request: Request,
) -> None:
    await check_org_selfie_step_up(request, ctx, action="payment_method_change")
    pm = (
        await ctx.session.execute(
            sa.select(PaymentMethod).where(
                PaymentMethod.org_id == ctx.org.id,
                PaymentMethod.id == payment_method_id,
            )
        )
    ).scalar_one_or_none()
    if pm is None:
        raise NotFoundError("Payment method not found")

    auto = ctx.org.credit_auto_recharge
    if auto and str(payment_method_id) == str(auto.get("payment_method_id")):
        raise ValidationFailedError("Turn off auto-recharge before removing this card.")

    settings = request.app.state.settings
    brand = pm.brand
    last4 = pm.last4
    was_default = pm.is_default

    await stripe_client.detach_payment_method(
        settings,
        payment_method_id=pm.stripe_payment_method_id,
    )

    await ctx.session.delete(pm)
    await ctx.session.flush()  # apply delete before querying the remaining cards

    if was_default:
        # Promote the oldest remaining card to default in the same transaction.
        remaining = (
            await ctx.session.execute(
                sa.select(PaymentMethod)
                .where(PaymentMethod.org_id == ctx.org.id)
                .order_by(PaymentMethod.created_at.asc(), PaymentMethod.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if remaining is not None:
            remaining.is_default = True

    actor_user, actor_key = _actor(ctx)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="billing.payment_method_deleted",
        target_type="payment_method",
        target_id=str(payment_method_id),
        actor_user_id=actor_user,
        actor_api_key_id=actor_key,
        detail={"brand": brand, "last4": last4},
    )
    await ctx.session.commit()
    return None
