
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
from pydantic import BaseModel

from app.auth.deps import OrgContext, require_permission
from app.errors import NotFoundError, ValidationFailedError
from app.models import Call, CreditLedgerEntry, PaymentMethod
from app.services import ai_usage
from app.services import audit as audit_svc
from app.services import credits
from app.services import spend as spend_svc
from app.services import stripe_client

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


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


class AutoRechargeIn(BaseModel):
    enabled: bool
    threshold_micros: int | None = None
    amount_micros: int | None = None
    payment_method_id: uuid.UUID | None = None


class PaymentMethodIn(BaseModel):
    stripe_payment_method_id: str
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


@router.get("/summary")
async def get_summary(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
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
        "warning": credits.warning_level(
            balance,
            int(last_topup.amount_micros) if last_topup is not None else 0,
        ),
        "auto_recharge": ctx.org.credit_auto_recharge,
        "last_topup": last_topup_dict,
        "fallback": ai_usage.credit_fallback(ctx.org),
    }


@router.get("/ledger")
async def get_ledger(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
    limit: int = Query(50, ge=1),
    cursor: str | None = None,
) -> dict:
    limit = min(limit, 200)

    stmt = sa.select(CreditLedgerEntry).where(
        CreditLedgerEntry.org_id == ctx.org.id
    )

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
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
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
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
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
    if amount not in exact_amounts and not (
        5_000_000 <= amount <= 5_000_000_000
    ):
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
        if payload.amount_micros < 5_000_000:
            raise ValidationFailedError("Auto-recharge amount must be at least $5.")

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


@router.get("/rates")
async def get_rates(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
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
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> list[dict]:
    rows = (
        await ctx.session.execute(
            sa.select(PaymentMethod)
            .where(PaymentMethod.org_id == ctx.org.id)
            .order_by(PaymentMethod.is_default.desc(), PaymentMethod.created_at.asc())
        )
    ).scalars().all()

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

    # The org has no stripe_customer_id column (Fable owns the schema), so the
    # customer id is carried on payment_method rows and re-used from there.
    existing_rows = (
        await ctx.session.execute(
            sa.select(PaymentMethod)
            .where(PaymentMethod.org_id == ctx.org.id)
            .order_by(PaymentMethod.created_at.desc(), PaymentMethod.id.desc())
        )
    ).scalars().all()
    existing_customer_id = payload.customer_id or (
        existing_rows[0].stripe_customer_id if existing_rows else None
    )
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

    pm = PaymentMethod(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        stripe_customer_id=customer_id,
        stripe_payment_method_id=attached["id"],
        brand=attached.get("brand", ""),
        last4=attached.get("last4", ""),
        is_default=is_default,
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
        raise ValidationFailedError(
            "Turn off auto-recharge before removing this card."
        )

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
