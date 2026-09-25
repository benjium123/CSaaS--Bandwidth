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
from app.models import Org, PlatformPrice
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
        "auto_recharge", "sms_bundle_units", "mms_bundle_units", "numbers",
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


class AdjustIn(BaseModel):
    amount_micros: int = Field(ge=-100_000_000_000, le=100_000_000_000)
    note: str = Field(min_length=3, max_length=255)


@router.post("/orgs/{org_id}/adjust")
async def console_adjust(org_id: uuid.UUID, payload: AdjustIn, op: Admin) -> dict:
    """Manual credit (positive) or debit (negative) with a reason - e.g. launch credit."""
    from app.services import credits

    if payload.amount_micros == 0:
        raise ValidationFailedError("Amount cannot be zero")
    if await op.session.get(Org, org_id) is None:
        raise NotFoundError("Organisation not found")
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
    kind: str = Field(pattern="^(sms|mms)$")
    units: int = Field(ge=1, le=10_000_000)
    note: str = Field(min_length=3, max_length=255)


@router.post("/orgs/{org_id}/bundles")
async def console_grant_bundle(org_id: uuid.UUID, payload: BundleGrantIn, op: Admin) -> dict:
    from app.services import bundles

    if await op.session.get(Org, org_id) is None:
        raise NotFoundError("Organisation not found")
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
