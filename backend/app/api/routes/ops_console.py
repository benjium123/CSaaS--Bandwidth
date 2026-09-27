"""Admin console (billing v2): full visibility over every organisation - purchases,
discounts, usage, traffic, blocked attempts and profit - plus the price list and manual
credit tools. Named operators only; reviewers read, admins change money."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, timezone
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from app.auth.deps import OperatorContext, require_operator
from app.db.base import set_org_context
from app.errors import NotFoundError, ValidationFailedError
from app.models import FixedCost, Org, PlatformPrice
from app.services import console

router = APIRouter(prefix="/api/v1/ops/console", tags=["ops-console"])
log = structlog.get_logger("ops_console")

Reviewer = Annotated[OperatorContext, Depends(require_operator("reviewer"))]
Admin = Annotated[OperatorContext, Depends(require_operator("admin"))]
Start = Annotated[date | None, Query()]
End = Annotated[date | None, Query()]


def _audit(op: OperatorContext, org_id, action: str, detail: dict) -> None:  # noqa: ANN001
    from app.services import audit as audit_svc

    set_org_context(op.session, org_id)
    audit_svc.record(
        op.session,
        org_id,
        action=action,
        target_type="org",
        target_id=str(org_id),
        actor_user_id=op.user.id,
        detail={"operator_user_id": str(op.user.id), **detail},
    )


@router.get("/orgs")
async def console_orgs(op: Reviewer, start: Start = None, end: End = None) -> dict:
    return await console.orgs_table(op.session, start, end)


@router.get("/orgs.csv")
async def console_orgs_csv(op: Reviewer, start: Start = None, end: End = None) -> Response:
    data = await console.orgs_table(op.session, start, end)
    metric_keys = sorted({k for r in data["orgs"] for k in r["metrics"]})
    base = [
        "org_id", "name", "slug", "prepaid", "billing_state", "balance_micros",
        "auto_recharge", "sms_bundle_units", "mms_bundle_units", "voice_bundle_minutes", "numbers",
    ]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(base + metric_keys)
    for r in data["orgs"]:
        w.writerow([r[k] for k in base] + [r["metrics"].get(k, 0) for k in metric_keys])
    return Response(
        buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="orgs_{data["start"]}_{data["end"]}.csv"'
        },
    )


@router.get("/orgs/{org_id}")
async def console_org(
    org_id: uuid.UUID, op: Reviewer, start: Start = None, end: End = None
) -> dict:
    detail = await console.org_detail(op.session, org_id, start, end)
    if detail is None:
        raise NotFoundError("Organisation not found")
    return detail


@router.get("/payments")
async def console_payments(op: Reviewer, start: Start = None, end: End = None) -> dict:
    return {"payments": await console.payments_list(op.session, start, end)}


@router.get("/prices")
async def console_prices(op: Reviewer) -> dict:
    from app.services.telephony_billing import PLATFORM_PRICE_MICROS

    rows = {
        r.metric: r for r in (await op.session.execute(sa.select(PlatformPrice))).scalars()
    }
    out = []
    for metric in sorted(set(PLATFORM_PRICE_MICROS) | set(rows)):
        r = rows.get(metric)
        out.append(
            {
                "metric": metric,
                "price_micros": int(r.price_micros) if r else PLATFORM_PRICE_MICROS[metric],
                "default_micros": PLATFORM_PRICE_MICROS.get(metric),
                "note": r.note if r else None,
                "updated_at": r.updated_at.isoformat() if r and r.updated_at else None,
            }
        )
    return {"prices": out}


class PriceIn(BaseModel):
    price_micros: int = Field(ge=0, le=1_000_000_000)
    note: str | None = Field(default=None, max_length=255)


@router.put("/prices/{metric}")
async def console_set_price(metric: str, payload: PriceIn, op: Admin) -> dict:
    from app.services.telephony_billing import PLATFORM_PRICE_MICROS

    if metric not in PLATFORM_PRICE_MICROS:
        raise ValidationFailedError(f"Unknown price: {metric}")
    row = await op.session.get(PlatformPrice, metric)
    old = int(row.price_micros) if row else PLATFORM_PRICE_MICROS[metric]
    if row is None:
        row = PlatformPrice(metric=metric, price_micros=payload.price_micros)
        op.session.add(row)
    row.price_micros = payload.price_micros
    if payload.note is not None:
        row.note = payload.note
    row.updated_by = op.user.id
    row.updated_at = datetime.now(timezone.utc)
    # Platform-wide, not org-scoped: the row carries updated_by/updated_at and the change
    # is logged (audit.record needs an org).
    log.info(
        "platform_price_updated",
        metric=metric,
        old_micros=old,
        new_micros=payload.price_micros,
        operator_user_id=str(op.user.id),
    )
    await op.session.commit()
    return {"metric": metric, "price_micros": payload.price_micros, "previous_micros": old}


class FeatureIn(BaseModel):
    enabled: bool
    price_override_micros: int | None = Field(default=None, ge=0, le=1_000_000_000)


async def _org_or_404(op: OperatorContext, org_id: uuid.UUID) -> Org:
    org = (
        await op.session.execute(
            sa.select(Org).where(Org.id == org_id).execution_options(allow_unscoped=True)
        )
    ).scalar_one_or_none()
    if org is None:
        raise NotFoundError("Workspace not found")
    return org


@router.get("/orgs/{org_id}/features")
async def console_org_features(org_id: uuid.UUID, op: Reviewer) -> dict:
    from app.services import entitlements

    from app.services import calling_settings

    org = await _org_or_404(op, org_id)
    values = await entitlements.for_org(op.session, org_id)
    return {
        "org_id": str(org_id),
        # The "this call may be recorded" notice: on by default, super admins switch it.
        "recording_notice": {
            "enabled": not calling_settings.announcement_ops_off(org),
            "record_calls": calling_settings.record_calls_for(org),
        },
        "features": [
            {
                "key": f.key,
                "label": f.label,
                "group": f.group,
                "description": f.description,
                "default_enabled": f.default_enabled,
                "enabled": values[f.key],
                "price_metric": f.price_metric,
            }
            for f in entitlements.CATALOG.values()
        ],
    }


@router.put("/orgs/{org_id}/features/{key}")
async def console_set_org_feature(
    org_id: uuid.UUID, key: str, payload: FeatureIn, op: Admin
) -> dict:
    from app.services import entitlements

    if key not in entitlements.CATALOG:
        raise ValidationFailedError(f"Unknown feature: {key}")
    await _org_or_404(op, org_id)
    before = await entitlements.has(op.session, org_id, key)
    await entitlements.set_feature(
        op.session,
        org_id,
        key,
        enabled=payload.enabled,
        price_override_micros=payload.price_override_micros,
        actor_user_id=op.user.id,
    )
    _audit(
        op,
        org_id,
        "org_feature.updated",
        {"feature": key, "from": before, "to": payload.enabled,
         "price_override_micros": payload.price_override_micros},
    )
    await op.session.commit()
    return {"org_id": str(org_id), "key": key, "enabled": payload.enabled}


class RecordingNoticeIn(BaseModel):
    enabled: bool


@router.put("/orgs/{org_id}/recording-notice")
async def console_set_recording_notice(
    org_id: uuid.UUID, payload: RecordingNoticeIn, op: Admin
) -> dict:
    """Super admins only: switch the org's recording notice on or off. It plays only on
    calls the org records; the org itself cannot change this."""
    from app.services import calling_settings

    org = await _org_or_404(op, org_id)
    before = not calling_settings.announcement_ops_off(org)
    stored = dict(org.calling_settings or {})
    if payload.enabled:
        stored.pop("announcement_off", None)
    else:
        stored["announcement_off"] = True
    org.calling_settings = stored
    _audit(op, org_id, "org_recording_notice.updated", {"from": before, "to": payload.enabled})
    await op.session.commit()
    return {
        "org_id": str(org_id),
        "enabled": payload.enabled,
        "plays_now": calling_settings.announcement_enabled(org),
    }


class DiscountIn(BaseModel):
    #: One or more categories get the same percentage in one go.
    categories: list[str] = Field(min_length=1, max_length=5)
    percent: float = Field(gt=0, le=100)
    ends_at: datetime | None = None
    note: str | None = Field(default=None, max_length=255)


async def _discounts_payload(op: OperatorContext, org_id: uuid.UUID, **extra) -> dict:  # noqa: ANN003
    from app.services import discounts

    return {
        "org_id": str(org_id),
        "categories": list(discounts.CATEGORIES),
        "discounts": await discounts.for_org(op.session, org_id),
        **extra,
    }


async def _sync_stripe(op: OperatorContext, request: Request, org_id: uuid.UUID) -> str:
    """Push subscription/numbers percentages to the Stripe plan subscription. Never fails
    the operator's change: the percentage is saved either way and applies in-app now."""
    from app.services import discounts

    try:
        synced = await discounts.sync_subscription(
            op.session, request.app.state.settings, org_id
        )
        await op.session.commit()
    except Exception:
        log.error("org_discount_sync_failed", org_id=str(org_id), exc_info=True)
        await op.session.rollback()
        return "failed"
    return "synced" if synced else "no_subscription"


@router.get("/orgs/{org_id}/discounts")
async def console_org_discounts(org_id: uuid.UUID, op: Reviewer) -> dict:
    await _org_or_404(op, org_id)
    return await _discounts_payload(op, org_id)


@router.put("/orgs/{org_id}/discounts")
async def console_set_org_discounts(
    org_id: uuid.UUID, payload: DiscountIn, op: Admin, request: Request
) -> dict:
    from app.services import discounts

    categories = list(dict.fromkeys(payload.categories))
    for category in categories:
        if category not in discounts.CATEGORIES:
            raise ValidationFailedError(f"Unknown discount category: {category}")
    await _org_or_404(op, org_id)
    bps = round(payload.percent * 100)
    changes = []
    for category in categories:
        _row, previous = await discounts.set_discount(
            op.session,
            org_id,
            category,
            percent_bps=bps,
            ends_at=payload.ends_at,
            note=payload.note,
            actor_user_id=op.user.id,
        )
        changes.append({"category": category, "from_bps": previous, "to_bps": bps})
    _audit(
        op,
        org_id,
        "org_discount.updated",
        {
            "changes": changes,
            "ends_at": payload.ends_at.isoformat() if payload.ends_at else None,
            "note": payload.note,
        },
    )
    await op.session.commit()
    stripe = None
    if {"subscription", "numbers"} & set(categories):
        stripe = await _sync_stripe(op, request, org_id)
    discounts.invalidate(op.session, org_id)
    return await _discounts_payload(op, org_id, stripe=stripe)


@router.delete("/orgs/{org_id}/discounts/{category}")
async def console_remove_org_discount(
    org_id: uuid.UUID, category: str, op: Admin, request: Request
) -> dict:
    from app.services import discounts

    await _org_or_404(op, org_id)
    row = await discounts.remove(op.session, org_id, category)
    if row is None:
        raise NotFoundError("That workspace has no discount on this category")
    _audit(
        op,
        org_id,
        "org_discount.removed",
        {"category": category, "from_bps": int(row.percent_bps)},
    )
    await op.session.commit()
    stripe = None
    if category in ("subscription", "numbers"):
        stripe = await _sync_stripe(op, request, org_id)
    discounts.invalidate(op.session, org_id)
    return await _discounts_payload(op, org_id, stripe=stripe)


class FixedCostIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    monthly_micros: int = Field(ge=0, le=100_000_000_000)
    starts_on: date
    ends_on: date | None = None
    note: str | None = Field(default=None, max_length=255)


def _fixed_cost_dict(row: FixedCost) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "monthly_micros": int(row.monthly_micros),
        "starts_on": row.starts_on.isoformat(),
        "ends_on": row.ends_on.isoformat() if row.ends_on else None,
        "note": row.note,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/fixed-costs")
async def console_fixed_costs(op: Reviewer) -> dict:
    rows = (
        (await op.session.execute(sa.select(FixedCost).order_by(FixedCost.starts_on)))
        .scalars()
        .all()
    )
    return {"fixed_costs": [_fixed_cost_dict(r) for r in rows]}


def _check_dates(payload: FixedCostIn) -> None:
    if payload.ends_on is not None and payload.ends_on < payload.starts_on:
        raise ValidationFailedError("The end date must be on or after the start date")


@router.post("/fixed-costs", status_code=201)
async def console_add_fixed_cost(payload: FixedCostIn, op: Admin) -> dict:
    _check_dates(payload)
    row = FixedCost(
        **payload.model_dump(), updated_by=op.user.id, updated_at=datetime.now(timezone.utc)
    )
    op.session.add(row)
    log.info("fixed_cost_added", name=payload.name, monthly_micros=payload.monthly_micros,
             operator_user_id=str(op.user.id))
    await op.session.commit()
    return _fixed_cost_dict(row)


@router.put("/fixed-costs/{cost_id}")
async def console_update_fixed_cost(cost_id: uuid.UUID, payload: FixedCostIn, op: Admin) -> dict:
    _check_dates(payload)
    row = await op.session.get(FixedCost, cost_id)
    if row is None:
        raise NotFoundError("Fixed cost not found")
    old = _fixed_cost_dict(row)
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    row.updated_by = op.user.id
    row.updated_at = datetime.now(timezone.utc)
    log.info("fixed_cost_updated", cost_id=str(cost_id), old=old,
             new=payload.model_dump(mode="json"), operator_user_id=str(op.user.id))
    await op.session.commit()
    return _fixed_cost_dict(row)


@router.delete("/fixed-costs/{cost_id}", status_code=204)
async def console_delete_fixed_cost(cost_id: uuid.UUID, op: Admin) -> Response:
    row = await op.session.get(FixedCost, cost_id)
    if row is None:
        raise NotFoundError("Fixed cost not found")
    log.info("fixed_cost_deleted", cost=_fixed_cost_dict(row), operator_user_id=str(op.user.id))
    await op.session.delete(row)
    await op.session.commit()
    return Response(status_code=204)


class AdjustIn(BaseModel):
    amount_micros: int = Field(ge=-100_000_000_000, le=100_000_000_000)
    note: str = Field(min_length=3, max_length=255)


@router.post("/orgs/{org_id}/adjust")
async def console_adjust(
    org_id: uuid.UUID, payload: AdjustIn, op: Admin, request: Request
) -> dict:
    """Manual credit (positive) or debit (negative) with a reason - e.g. launch credit."""
    from app.services import credits

    if payload.amount_micros == 0:
        raise ValidationFailedError("Amount cannot be zero")
    if await op.session.get(Org, org_id) is None:
        raise NotFoundError("Organisation not found")
    from app.services import grant_approval

    if await grant_approval.needs_second_operator_for_credit(
        op.session, request.app.state.settings, op.user.id, payload.amount_micros
    ):
        pending_id = await grant_approval.request(
            op.session,
            org_id,
            op.user.id,
            {"type": "credit", "amount_micros": payload.amount_micros, "note": payload.note},
        )
        _audit(op, org_id, "billing.console_adjustment_requested",
               {"amount_micros": payload.amount_micros, "note": payload.note})
        await op.session.commit()
        return {"pending_approval": str(pending_id)}
    set_org_context(op.session, org_id)
    entry = await credits.adjust(
        op.session,
        org_id,
        payload.amount_micros,
        reference=f"console:{uuid.uuid4()}",
        note=payload.note,
        created_by=op.user.id,
    )
    _audit(op, org_id, "billing.console_adjustment",
           {"amount_micros": payload.amount_micros, "note": payload.note})
    await op.session.commit()
    return {"balance_after_micros": int(entry.balance_after_micros)}


class BundleGrantIn(BaseModel):
    kind: str = Field(pattern="^(sms|mms|voice)$")
    units: int = Field(ge=1, le=10_000_000)
    note: str = Field(min_length=3, max_length=255)


@router.post("/orgs/{org_id}/bundles")
async def console_grant_bundle(org_id: uuid.UUID, payload: BundleGrantIn, op: Admin) -> dict:
    from app.services import bundles

    if await op.session.get(Org, org_id) is None:
        raise NotFoundError("Organisation not found")
    from app.services import grant_approval

    if grant_approval.needs_second_operator_for_bundle(payload.kind, payload.units):
        pending_id = await grant_approval.request(
            op.session,
            org_id,
            op.user.id,
            {"type": "bundle", "kind": payload.kind, "units": payload.units, "note": payload.note},
        )
        _audit(op, org_id, "billing.console_bundle_grant_requested",
               {"kind": payload.kind, "units": payload.units, "note": payload.note})
        await op.session.commit()
        return {"pending_approval": str(pending_id)}
    entry = await bundles.credit(
        op.session,
        org_id,
        payload.kind,
        payload.units,
        reference=f"console:{uuid.uuid4()}",
        note=payload.note,
        entry_type="adjustment",
    )
    _audit(op, org_id, "billing.console_bundle_grant",
           {"kind": payload.kind, "units": payload.units, "note": payload.note})
    await op.session.commit()
    return {"units_after": int(entry.balance_after_units)}


class PrepaidIn(BaseModel):
    enabled: bool


@router.post("/orgs/{org_id}/prepaid")
async def console_prepaid(org_id: uuid.UUID, payload: PrepaidIn, op: Admin) -> dict:
    """Switch an org's prepaid gate. Switching ON bills from now only (never retro)."""
    from app.services import telephony_billing

    org = await op.session.get(Org, org_id)
    if org is None:
        raise NotFoundError("Organisation not found")
    if payload.enabled and not org.telephony_prepaid:
        org.telephony_prepaid = True
        org.telephony_prepaid_since = datetime.now(timezone.utc)
        await telephony_billing.stamp_rentals_forward(op.session, org_id)
    elif not payload.enabled:
        org.telephony_prepaid = False
    _audit(op, org_id, "billing.console_prepaid", {"enabled": payload.enabled})
    await op.session.commit()
    return {"prepaid": bool(org.telephony_prepaid)}


@router.get("/telnyx")
async def console_telnyx(op: Reviewer, request: Request) -> dict:
    """Telnyx account float (shared with the CRM) and the last reconciled days."""
    from app.models import TelnyxCostDaily
    from app.services import telnyx_recon

    settings = request.app.state.settings
    balance = None
    key = await telnyx_recon.api_key(op.session, settings)
    if key:
        billing = telnyx_recon.TelnyxBilling(key)
        try:
            balance = await billing.balance()
        except Exception:
            log.warning("console_telnyx_balance_failed")
        finally:
            await billing.aclose()
    rows = (
        await op.session.execute(
            sa.select(
                TelnyxCostDaily.period_date,
                sa.func.sum(TelnyxCostDaily.cost_micros),
                sa.func.sum(
                    sa.case((TelnyxCostDaily.org_id.is_(None), TelnyxCostDaily.cost_micros), else_=0)
                ),
            )
            .group_by(TelnyxCostDaily.period_date)
            .order_by(TelnyxCostDaily.period_date.desc())
            .limit(31)
        )
    ).all()
    return {
        "balance": balance,
        "days": [
            {"date": d.isoformat(), "cost_micros": int(c or 0), "unattributed_micros": int(u or 0)}
            for d, c, u in rows
        ],
    }


@router.get("/grants/pending")
async def console_pending_grants(op: Admin) -> list[dict]:
    """P44d: credit and bundle grants waiting for a second operator."""
    from app.services import grant_approval

    rows = await grant_approval.pending(op.session)
    return [
        {
            "id": str(r.id),
            "org_id": str(r.org_id),
            "requested_at": r.created_at.isoformat() if r.created_at else None,
            **dict(r.detail or {}),
        }
        for r in rows
    ]


class GrantDecisionIn(BaseModel):
    approve: bool


@router.post("/grants/{alert_id}/decide")
async def console_decide_grant(alert_id: uuid.UUID, payload: GrantDecisionIn, op: Admin) -> dict:
    """P44d: a DIFFERENT admin approves (applies) or rejects a pending grant."""
    from app.services import grant_approval

    result = await grant_approval.decide(
        op.session, alert_id, op.user.id, approve=payload.approve
    )
    await op.session.commit()
    log.info(
        "console_grant_decided",
        alert_id=str(alert_id),
        approve=payload.approve,
        operator_user_id=str(op.user.id),
    )
    return result


@router.get("/audit")
async def console_audit(
    op: Admin,
    operator_user_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict:
    """H1: every change a platform operator (or the shared ops token) made, newest first.
    ``q`` matches the route; ``before`` pages back (pass the last row's ``at``)."""
    from app.models import OperatorAuditEntry as E

    stmt = sa.select(E).order_by(E.at.desc(), E.id.desc()).limit(limit)
    if operator_user_id is not None:
        stmt = stmt.where(E.operator_user_id == operator_user_id)
    if org_id is not None:
        stmt = stmt.where(E.org_id == org_id)
    if q:
        stmt = stmt.where(E.route.ilike(f"%{q.strip()}%"))
    if before is not None:
        stmt = stmt.where(E.at < before)
    rows = (await op.session.execute(stmt)).scalars().all()
    return {
        "entries": [
            {
                "id": r.id,
                "at": r.at,
                "operator_user_id": r.operator_user_id,
                "operator_email": r.operator_email,
                "operator_role": r.operator_role,
                "method": r.method,
                "route": r.route,
                "path_params": r.path_params,
                "org_id": r.org_id,
                "status_code": r.status_code,
                "reason": r.reason,
                "ip": r.ip,
            }
            for r in rows
        ]
    }
