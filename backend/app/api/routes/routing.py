"""Carrier routing: what we can send through, and how this org wants to choose.

Read is `settings:read`, write is `settings:write`. Routing decides which brand a recipient
sees and which registration a message is billed against, so it is an org setting rather
than an inbox one - an agent should not be able to move traffic between carriers.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.auth.deps import OrgContext, require_permission
from app.errors import ValidationFailedError
from app.routing import router as routing_svc

router = APIRouter(prefix="/api/v1/routing", tags=["routing"])


class CarrierStatusOut(BaseModel):
    name: str
    primary: bool
    state: str
    consecutive_failures: int
    capabilities: dict


class CarrierCatalogOut(BaseModel):
    """Every carrier this BUILD supports, live or not - so an operator can see what is
    available to switch on, not merely what is already on."""

    name: str
    live: bool
    #: Why not, and exactly which variables would fix it. Never a value.
    reason: str
    missing: list[str]
    #: Explicit override state: true/false pinned, or None = auto (keys decide).
    enabled_flag: bool | None
    #: Present only when the carrier is live and therefore built.
    primary: bool = False
    state: str = ""
    capabilities: dict = {}
    supports_numbers: bool = False
    supports_voice: bool = False
    #: False when this deployment holds VOICE-only credentials for the carrier. The
    #: console shows it so an operator learns before they try to text from it.
    supports_messaging: bool = True


class ProbeOut(BaseModel):
    name: str
    ok: bool
    detail: str
    checked: str


class PolicyOut(BaseModel):
    preference: list[str]
    allow_intra_carrier_failover: bool
    allow_cross_carrier_failover: bool
    pinned_carrier: str | None


class PolicyIn(BaseModel):
    preference: list[str] | None = Field(default=None)
    allow_intra_carrier_failover: bool | None = None
    allow_cross_carrier_failover: bool | None = None
    pinned_carrier: str | None = None


def _registry(request: Request):
    return getattr(request.app.state, "carriers", None)


@router.get("/carriers", response_model=list[CarrierStatusOut])
async def list_carriers(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> list[CarrierStatusOut]:
    """Which carriers this deployment can actually use, and how each one is behaving.

    Never includes a credential - only whether one is present and working.
    """
    registry = _registry(request)
    return [CarrierStatusOut(**row) for row in (registry.status() if registry else [])]


@router.get("/catalog", response_model=list[CarrierCatalogOut])
async def carrier_catalog(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> list[CarrierCatalogOut]:
    """The bring-your-own-key view: every supported carrier, whether it is live, and if
    not, the exact variables that would make it live."""
    from app.providers.numbers import NumberProvider
    from app.providers.voice import VoiceCarrier

    settings = request.app.state.settings
    registry = _registry(request)
    built = {row["name"]: row for row in (registry.status() if registry else [])}
    statuses = {p.name: p for p in settings.provider_statuses()}

    out: list[CarrierCatalogOut] = []
    for name in settings.carrier_requirements():
        status = statuses.get(name)
        row = built.get(name)
        carrier = registry.get(name) if registry else None
        out.append(
            CarrierCatalogOut(
                name=name,
                live=bool(row),
                reason=("" if row else (status.reason if status else "not configured")),
                missing=list(getattr(status, "missing", []) or []),
                enabled_flag=settings.carrier_flag(name),
                primary=bool(row and row.get("primary")),
                state=(row or {}).get("state", ""),
                capabilities=(row or {}).get("capabilities", {}),
                supports_numbers=isinstance(carrier, NumberProvider),
                supports_voice=isinstance(carrier, VoiceCarrier),
                supports_messaging=(
                    carrier.capabilities.supports_messaging if carrier is not None else True
                ),
            )
        )
    return out


@router.post("/carriers/{name}/probe", response_model=ProbeOut)
async def probe_carrier(
    name: str,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> ProbeOut:
    """Ask the carrier whether these credentials work. Operator-triggered ONLY - see
    providers/probes.py for why this never runs on boot."""
    from app.providers import probes

    result = await probes.probe(name, request.app.state.settings)
    return ProbeOut(
        name=result.name, ok=result.ok, detail=result.detail, checked=result.checked
    )


@router.get("/policy", response_model=PolicyOut)
async def get_policy(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> PolicyOut:
    policy = await routing_svc.get_policy(ctx.session, ctx.org.id)
    await ctx.session.commit()
    return PolicyOut(
        preference=list(policy.preference or []),
        allow_intra_carrier_failover=policy.allow_intra_carrier_failover,
        allow_cross_carrier_failover=policy.allow_cross_carrier_failover,
        pinned_carrier=policy.pinned_carrier,
    )


@router.patch("/policy", response_model=PolicyOut)
async def update_policy(
    payload: PolicyIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> PolicyOut:
    registry = _registry(request)
    known = set(registry.names()) if registry else set()
    policy = await routing_svc.get_policy(ctx.session, ctx.org.id)

    if payload.preference is not None:
        # Refuse a preference naming a carrier this deployment cannot use. Accepting it
        # would store a policy that silently does nothing - the operator would believe
        # traffic had moved when it had not.
        unknown = [c for c in payload.preference if c not in known]
        if unknown:
            raise ValidationFailedError(
                f"Unknown carrier(s): {', '.join(unknown)}. Configured: "
                f"{', '.join(sorted(known)) or 'none'}"
            )
        policy.preference = list(payload.preference)

    if payload.pinned_carrier is not None:
        pinned = payload.pinned_carrier.strip()
        if pinned and pinned not in known:
            raise ValidationFailedError(
                f"Cannot pin to {pinned!r}: it is not configured on this deployment"
            )
        policy.pinned_carrier = pinned or None

    if payload.allow_intra_carrier_failover is not None:
        policy.allow_intra_carrier_failover = payload.allow_intra_carrier_failover
    if payload.allow_cross_carrier_failover is not None:
        policy.allow_cross_carrier_failover = payload.allow_cross_carrier_failover

    await ctx.session.commit()
    return PolicyOut(
        preference=list(policy.preference or []),
        allow_intra_carrier_failover=policy.allow_intra_carrier_failover,
        allow_cross_carrier_failover=policy.allow_cross_carrier_failover,
        pinned_carrier=policy.pinned_carrier,
    )
