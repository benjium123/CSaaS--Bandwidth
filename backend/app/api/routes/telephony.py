"""P37a: the three customer-facing managed-telephony endpoints.

Thin on purpose - every rule lives in ``services/telephony_provisioning.py``. What this
file owns is the SHAPE that reaches a customer, and the shape is deliberately austere: no
API key, no SIP password, no master key, and no raw Telnyx identifier. A managed account
id is of no use to the console and every id echoed back is one more thing a support
screenshot can leak, so ``status`` reports progress only.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.routes.numbers import NumberOut, _out
from app.auth.deps import OrgContext, require_permission
from app.services import audit as audit_svc
from app.services import telephony_provisioning as provisioning_svc

router = APIRouter(prefix="/api/v1/telephony", tags=["telephony"])


class StepOut(BaseModel):
    key: str
    done: bool


class TelephonyStatusOut(BaseModel):
    #: "not_started" | "provisioning" | "active" | "suspended" | "failed".
    status: str = "not_started"
    last_step: str | None = None
    #: A plain sentence, safe to show the customer. Never raw carrier output.
    last_error: str | None = None
    steps: list[StepOut] = []


class OrderIn(BaseModel):
    e164: str = Field(min_length=3, max_length=32)


def _status_out(account) -> TelephonyStatusOut:  # noqa: ANN001
    return TelephonyStatusOut(
        status=account.status if account is not None else "not_started",
        last_step=account.last_step if account is not None else None,
        last_error=account.last_error if account is not None else None,
        steps=[StepOut(**s) for s in provisioning_svc.steps_done(account)],
    )


@router.post("/setup", response_model=TelephonyStatusOut)
async def setup(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> TelephonyStatusOut:
    settings = request.app.state.settings
    provisioning_svc.require_managed_telephony(settings)
    try:
        account = await provisioning_svc.provision(ctx.session, settings, ctx.org.id)
    finally:
        # Audited whether it succeeded or failed - a run that stopped half way is
        # exactly the one an operator later needs to find. Never any secret: the detail
        # carries only progress, and last_error is already a plain sentence.
        current = await provisioning_svc.get_account(ctx.session, ctx.org.id)
        audit_svc.record(
            ctx.session,
            ctx.org.id,
            actor_user_id=ctx.actor_user_id,
            actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
            action="telephony.setup",
            target_type="telephony_account",
            target_id=str(current.id) if current is not None else None,
            detail={
                "status": current.status if current is not None else "not_started",
                "last_step": current.last_step if current is not None else None,
            },
        )
        await ctx.session.commit()
    return _status_out(account)


@router.get("/status", response_model=TelephonyStatusOut)
async def status(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> TelephonyStatusOut:
    provisioning_svc.require_managed_telephony(request.app.state.settings)
    return _status_out(await provisioning_svc.get_account(ctx.session, ctx.org.id))


@router.post("/numbers/order", response_model=NumberOut, status_code=201)
async def order_number(
    payload: OrderIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("numbers:manage"))],
) -> NumberOut:
    settings = request.app.state.settings
    provisioning_svc.require_managed_telephony(settings)
    number = await provisioning_svc.order_number(
        ctx.session, settings, ctx.org.id, payload.e164
    )
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key is not None else None,
        action="telephony.number_ordered",
        target_type="org_number",
        target_id=str(number.id),
        detail={"e164": number.e164, "carrier": number.carrier, "status": number.status},
    )
    await ctx.session.commit()
    return await _out(ctx.session, number)
