"""P12: call flows / ring groups / queues / business hours / voicemails / supervisor ops.

Read is `settings:read`, write is `settings:write` for configuration (flows, ring groups,
queues, business hours) - same convention as `routes/routing.py`: this is org configuration,
not something an agent should edit. Runtime queue-entry actions (claim, callback dial-now)
and voicemail triage use `calls:read`/`calls:place`, matching `routes/calls.py`. Supervisor
ops additionally require `calls:supervise` (Tier-1 schema pass, migration 0015).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.auth.deps import OrgContext, get_current_user, require_permission
from app.compliance import quiet_hours as qh
from app.compliance import service as compliance_svc
from app.compliance.gate import _contact_timezone as _gate_contact_timezone
from app.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationFailedError
from app.models import User
from app.models.callflow import (
    BusinessHours,
    CallFlow,
    CallQueue,
    QueueEntry,
    RingGroupDef,
    Voicemail,
)
from app.models.messaging import OrgNumber
from app.models.voice import Call
from app.services import calls as calls_svc
from app.services import flows as flows_svc
from app.services import routing_exec as routing_exec_svc
from app.services import supervisor as supervisor_svc

router = APIRouter(prefix="/api/v1", tags=["flows"])


# ==================================================================================
# Flows
# ==================================================================================
class FlowIn(BaseModel):
    name: str = Field(min_length=1, max_length=127)
    definition: dict


class FlowOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    status: str
    definition: dict
    created_at: datetime


def _flow_out(f: CallFlow) -> FlowOut:
    return FlowOut(
        id=f.id,
        name=f.name,
        version=f.version,
        status=f.status,
        definition=f.definition,
        created_at=f.created_at,
    )


@router.get("/flows", response_model=list[FlowOut])
async def list_flows(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> list[FlowOut]:
    return [_flow_out(r) for r in await flows_svc.list_flows(ctx.session)]


@router.post("/flows", response_model=FlowOut, status_code=201)
async def create_flow(
    payload: FlowIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
    user: Annotated[User, Depends(get_current_user)],
) -> FlowOut:
    row = await flows_svc.create_flow(
        ctx.session,
        ctx.org.id,
        name=payload.name,
        definition=payload.definition,
        created_by=user.id,
    )
    return _flow_out(row)


@router.get("/flows/{flow_id}", response_model=FlowOut)
async def get_flow(
    flow_id: uuid.UUID, ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))]
) -> FlowOut:
    return _flow_out(await flows_svc.get_flow(ctx.session, flow_id))


@router.get("/flows/by-name/{name}/versions", response_model=list[FlowOut])
async def list_flow_versions(
    name: str, ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))]
) -> list[FlowOut]:
    return [_flow_out(r) for r in await flows_svc.list_versions(ctx.session, name)]


class VersionIn(BaseModel):
    definition: dict


@router.post("/flows/{flow_id}/versions", response_model=FlowOut, status_code=201)
async def create_flow_version(
    flow_id: uuid.UUID,
    payload: VersionIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
    user: Annotated[User, Depends(get_current_user)],
) -> FlowOut:
    row = await flows_svc.create_version(
        ctx.session, ctx.org.id, flow_id=flow_id, definition=payload.definition, created_by=user.id
    )
    return _flow_out(row)


@router.post("/flows/{flow_id}/activate", response_model=FlowOut)
async def activate_flow(
    flow_id: uuid.UUID, ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))]
) -> FlowOut:
    return _flow_out(await flows_svc.activate_flow(ctx.session, ctx.org.id, flow_id))


class BindFlowIn(BaseModel):
    number_id: uuid.UUID
    flow_id: uuid.UUID | None = None


class NumberBindingOut(BaseModel):
    number_id: uuid.UUID
    e164: str
    call_flow_id: uuid.UUID | None


@router.post("/flows/bind", response_model=NumberBindingOut)
async def bind_flow(
    payload: BindFlowIn, ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))]
) -> NumberBindingOut:
    number = await flows_svc.bind_number(
        ctx.session, ctx.org.id, payload.number_id, payload.flow_id
    )
    return NumberBindingOut(number_id=number.id, e164=number.e164, call_flow_id=number.call_flow_id)


# ==================================================================================
# Ring groups
# ==================================================================================
class RingGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=127)
    strategy: str = "simultaneous"
    member_user_ids: list[uuid.UUID] = []
    ring_timeout_seconds: int = Field(default=20, ge=1, le=120)


class RingGroupOut(BaseModel):
    id: uuid.UUID
    name: str
    strategy: str
    member_user_ids: list[str]
    ring_timeout_seconds: int


def _ring_group_out(r: RingGroupDef) -> RingGroupOut:
    return RingGroupOut(
        id=r.id,
        name=r.name,
        strategy=r.strategy,
        member_user_ids=[str(m) for m in (r.member_user_ids or [])],
        ring_timeout_seconds=r.ring_timeout_seconds,
    )


@router.get("/ring-groups", response_model=list[RingGroupOut])
async def list_ring_groups(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> list[RingGroupOut]:
    return [_ring_group_out(r) for r in await flows_svc.list_ring_groups(ctx.session)]


@router.post("/ring-groups", response_model=RingGroupOut, status_code=201)
async def create_ring_group(
    payload: RingGroupIn, ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))]
) -> RingGroupOut:
    if payload.strategy not in ("simultaneous", "sequential"):
        raise ValidationFailedError("strategy must be 'simultaneous' or 'sequential'")
    row = await flows_svc.create_ring_group(
        ctx.session,
        ctx.org.id,
        name=payload.name,
        strategy=payload.strategy,
        member_user_ids=[str(u) for u in payload.member_user_ids],
        ring_timeout_seconds=payload.ring_timeout_seconds,
    )
    return _ring_group_out(row)


# ==================================================================================
# Queues
# ==================================================================================
class QueueIn(BaseModel):
    name: str = Field(min_length=1, max_length=127)
    hold_audio_url: str | None = None
    max_wait_seconds: int = Field(default=300, ge=10, le=3600)
    overflow: str = "voicemail"
    ring_group_id: uuid.UUID | None = None


class QueueOut(BaseModel):
    id: uuid.UUID
    name: str
    hold_audio_url: str | None
    max_wait_seconds: int
    overflow: str
    ring_group_id: uuid.UUID | None


def _queue_out(q: CallQueue) -> QueueOut:
    return QueueOut(
        id=q.id,
        name=q.name,
        hold_audio_url=q.hold_audio_url,
        max_wait_seconds=q.max_wait_seconds,
        overflow=q.overflow,
        ring_group_id=q.ring_group_id,
    )


@router.get("/queues", response_model=list[QueueOut])
async def list_queues(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> list[QueueOut]:
    return [_queue_out(q) for q in await flows_svc.list_queues(ctx.session)]


@router.post("/queues", response_model=QueueOut, status_code=201)
async def create_queue(
    payload: QueueIn, ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))]
) -> QueueOut:
    if payload.overflow not in ("voicemail", "hangup", "callback"):
        raise ValidationFailedError("overflow must be 'voicemail', 'hangup', or 'callback'")
    row = await flows_svc.create_queue(
        ctx.session,
        ctx.org.id,
        name=payload.name,
        hold_audio_url=payload.hold_audio_url,
        max_wait_seconds=payload.max_wait_seconds,
        overflow=payload.overflow,
        ring_group_id=payload.ring_group_id,
    )
    return _queue_out(row)


class QueueEntryOut(BaseModel):
    id: uuid.UUID
    queue_id: uuid.UUID
    call_id: uuid.UUID
    state: str
    offered_user_id: uuid.UUID | None
    callback_e164: str | None
    position: int | None
    enqueued_at: datetime
    resolved_at: datetime | None


def _entry_out(e: QueueEntry, *, position: int | None = None) -> QueueEntryOut:
    return QueueEntryOut(
        id=e.id,
        queue_id=e.queue_id,
        call_id=e.call_id,
        state=e.state,
        offered_user_id=e.offered_user_id,
        callback_e164=e.callback_e164,
        position=position,
        enqueued_at=e.enqueued_at,
        resolved_at=e.resolved_at,
    )


@router.get("/queues/{queue_id}/entries", response_model=list[QueueEntryOut])
async def list_queue_entries(
    queue_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:read"))],
    state: str | None = Query(default=None),
) -> list[QueueEntryOut]:
    states = [state] if state else None
    rows = await routing_exec_svc.list_queue_entries(ctx.session, queue_id, states=states)
    out = []
    for row in rows:
        position = (
            await routing_exec_svc.queue_position(ctx.session, row)
            if row.state == "waiting"
            else None
        )
        out.append(_entry_out(row, position=position))
    return out


@router.post("/queues/{queue_id}/claim-next", response_model=QueueEntryOut)
async def claim_next(
    queue_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:place"))],
    user: Annotated[User, Depends(get_current_user)],
) -> QueueEntryOut:
    entry = await routing_exec_svc.claim_next(ctx.session, ctx.org.id, queue_id, user.id)
    if entry is None:
        raise NotFoundError("No waiting entries in this queue")
    return _entry_out(entry)


@router.post("/queue-entries/{entry_id}/claim", response_model=QueueEntryOut)
async def claim_entry(
    entry_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:place"))],
    user: Annotated[User, Depends(get_current_user)],
) -> QueueEntryOut:
    entry = await routing_exec_svc.claim_entry(ctx.session, entry_id, user.id)
    # Same event shape `POST /calls/{id}/answer` already publishes (routes/calls.py) - any
    # console built against that broadcast still clears this ring card on a queue claim.
    request.app.state.event_bus.publish(
        ctx.org.id, {"type": "call.handoff.claimed", "call_id": str(entry.call_id)}
    )
    return _entry_out(entry)


@router.post("/queue-entries/{entry_id}/dial-now", response_model=QueueEntryOut)
async def dial_callback_now(
    entry_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:place"))],
    user: Annotated[User, Depends(get_current_user)],
) -> QueueEntryOut:
    """DR-6 v1: a manual "dial now" button for a captured callback. Places the call through
    the P5 outbound primitive (`services.calls.create_outbound_call`) - full P11
    campaign-scale auto-dialing for a single ad-hoc callback is future work (OPEN_ISSUES);
    this is the honest v1 scope the plan calls for.

    B8 fixes:
    - Runs the SAME compliance primitives `services/dialer.py` uses before every campaign
      dial (opt-out -> DNC -> quiet hours) - a captured callback number is exactly as
      protected as any other outbound dial, not exempt just because a human clicked a
      button. 409s (not silently skips) when blocked.
    - Caller ID prefers the number the caller ORIGINALLY dialed
      (`entry.call_id -> Call.our_e164`) - falls back to "any active number" only when
      that specific number is no longer active/owned by this org.
    - `entry.state` stays "callback_requested" through the dial itself. Advancing it to
      "connected" only once the outbound call is genuinely ANSWERED would need a hook in
      the voice-webhook path this implementer cannot add outside the P6 branch already
      owned (`routes/webhooks.py`) - so the smallest HONEST design, rather than lying
      about the state, is to stamp `offered_user_id`/`offered_at` as a "dialing in
      flight" marker (a console can show "dialing" by state still being
      callback_requested with a recent offered_at) and leave true state advancement as an
      OPEN_ISSUES follow-up once that webhook hook exists.
    """
    entry = await ctx.session.get(QueueEntry, entry_id)
    if entry is None or entry.state != "callback_requested" or not entry.callback_e164:
        raise NotFoundError("No callback-requested entry found")

    now = qh._now()  # noqa: SLF001 - same module-level, test-frozen clock qh.evaluate uses

    if await compliance_svc.is_opted_out(ctx.session, entry.callback_e164):
        raise ConflictError(f"{entry.callback_e164} has opted out - cannot dial")
    if await compliance_svc.is_dnc(ctx.session, entry.callback_e164):
        raise ConflictError(f"{entry.callback_e164} is on the DNC list - cannot dial")

    settings_row = await compliance_svc.get_settings(ctx.session, ctx.org.id)
    if settings_row.quiet_hours_enforced:
        tz = await _gate_contact_timezone(ctx.session, entry.callback_e164)
        result = qh.evaluate(
            entry.callback_e164,
            contact_timezone=tz,
            window_start=settings_row.window_start,
            window_end=settings_row.window_end,
            now=now,
        )
        if not result.allowed:
            raise ConflictError(
                f"{entry.callback_e164} is in quiet hours until "
                f"{result.not_before.isoformat() if result.not_before else 'later'}"
            )

    # Caller ID: the number the caller originally dialed, if it's still ours and active -
    # only falls back to "any active number" when that specific one is gone.
    original_call = await ctx.session.get(Call, entry.call_id)
    number = None
    if original_call is not None:
        number = (
            await ctx.session.execute(
                sa.select(OrgNumber).where(
                    OrgNumber.e164 == original_call.our_e164, OrgNumber.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
    if number is None:
        number = (
            (await ctx.session.execute(sa.select(OrgNumber).where(OrgNumber.is_active.is_(True))))
            .scalars()
            .first()
        )
    if number is None:
        raise ConflictError("No active outbound number is available on this org")

    registry = getattr(request.app.state, "carriers", None)
    await calls_svc.create_outbound_call(
        ctx.session,
        registry,
        ctx.org.id,
        to=entry.callback_e164,
        from_=number.e164,
        carrier_name=number.carrier,
        tag=f"queue-callback:{entry.id}",
    )
    entry.offered_user_id = user.id
    entry.offered_at = now
    await ctx.session.commit()
    return _entry_out(entry)


# ==================================================================================
# Business hours
# ==================================================================================
class BusinessHoursIn(BaseModel):
    name: str = Field(default="default", max_length=127)
    timezone: str = Field(default="America/Chicago", max_length=64)
    schedule: dict = {}
    holidays: list[str] = []


class BusinessHoursOut(BaseModel):
    id: uuid.UUID
    name: str
    timezone: str
    schedule: dict
    holidays: list[str]


def _hours_out(h: BusinessHours) -> BusinessHoursOut:
    return BusinessHoursOut(
        id=h.id,
        name=h.name,
        timezone=h.timezone,
        schedule=h.schedule,
        holidays=list(h.holidays or []),
    )


@router.get("/business-hours", response_model=list[BusinessHoursOut])
async def list_business_hours(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> list[BusinessHoursOut]:
    return [_hours_out(h) for h in await flows_svc.list_business_hours(ctx.session)]


@router.post("/business-hours", response_model=BusinessHoursOut, status_code=201)
async def create_business_hours(
    payload: BusinessHoursIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> BusinessHoursOut:
    row = await flows_svc.create_business_hours(
        ctx.session,
        ctx.org.id,
        name=payload.name,
        timezone_name=payload.timezone,
        schedule=payload.schedule,
        holidays=payload.holidays,
    )
    return _hours_out(row)


# ==================================================================================
# Voicemails
# ==================================================================================
class VoicemailOut(BaseModel):
    id: uuid.UUID
    call_id: uuid.UUID
    recording_id: uuid.UUID | None
    greeting_node: str | None
    transcript: str | None
    transcript_status: str
    status: str
    created_at: datetime


def _vm_out(v: Voicemail) -> VoicemailOut:
    return VoicemailOut(
        id=v.id,
        call_id=v.call_id,
        recording_id=v.recording_id,
        greeting_node=v.greeting_node,
        transcript=v.transcript,
        transcript_status=v.transcript_status,
        status=v.status,
        created_at=v.created_at,
    )


@router.get("/voicemails", response_model=list[VoicemailOut])
async def list_voicemails(
    ctx: Annotated[OrgContext, Depends(require_permission("calls:read"))],
    status: str | None = Query(default=None),
) -> list[VoicemailOut]:
    stmt = sa.select(Voicemail).order_by(Voicemail.created_at.desc())
    if status:
        stmt = stmt.where(Voicemail.status == status)
    rows = (await ctx.session.execute(stmt)).scalars().all()
    return [_vm_out(v) for v in rows]


@router.post("/voicemails/{voicemail_id}/mark-read", response_model=VoicemailOut)
async def mark_voicemail_read(
    voicemail_id: uuid.UUID, ctx: Annotated[OrgContext, Depends(require_permission("calls:place"))]
) -> VoicemailOut:
    row = await ctx.session.get(Voicemail, voicemail_id)
    if row is None:
        raise NotFoundError("Voicemail not found")
    row.status = "read"
    await ctx.session.commit()
    return _vm_out(row)


# ==================================================================================
# Supervisor ops (DR-9)
# ==================================================================================
def _require_supervisor(ctx: OrgContext) -> None:
    if not ctx.role.grants("calls:supervise"):
        raise PermissionDeniedError("Requires permission: calls:supervise")


class SupervisorTokenOut(BaseModel):
    url: str
    token: str
    room: str


async def _get_call(ctx: OrgContext, call_id: uuid.UUID) -> Call:
    call = await ctx.session.get(Call, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    return call


@router.post("/calls/{call_id}/monitor", response_model=SupervisorTokenOut)
async def supervise_monitor(
    call_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:read"))],
    user: Annotated[User, Depends(get_current_user)],
) -> SupervisorTokenOut:
    _require_supervisor(ctx)
    call = await _get_call(ctx, call_id)
    settings = request.app.state.settings
    token = await supervisor_svc.monitor(
        ctx.session,
        settings,
        call,
        identity=f"supervisor-{user.id}",
        name=user.email,
        actor_user_id=user.id,
    )
    return SupervisorTokenOut(
        url=settings.livekit_public_url or settings.livekit_url,
        token=token,
        room=(call.extra or {}).get("room", ""),
    )


@router.post("/calls/{call_id}/whisper", response_model=SupervisorTokenOut)
async def supervise_whisper(
    call_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:read"))],
    user: Annotated[User, Depends(get_current_user)],
) -> SupervisorTokenOut:
    _require_supervisor(ctx)
    call = await _get_call(ctx, call_id)
    settings = request.app.state.settings
    api = getattr(request.app.state, "livekit", None)
    token = await supervisor_svc.whisper(
        ctx.session,
        settings,
        api,
        call,
        identity=f"supervisor-{user.id}",
        name=user.email,
        actor_user_id=user.id,
    )
    return SupervisorTokenOut(
        url=settings.livekit_public_url or settings.livekit_url,
        token=token,
        room=(call.extra or {}).get("room", ""),
    )


@router.post("/calls/{call_id}/barge", response_model=SupervisorTokenOut)
async def supervise_barge(
    call_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:read"))],
    user: Annotated[User, Depends(get_current_user)],
) -> SupervisorTokenOut:
    _require_supervisor(ctx)
    call = await _get_call(ctx, call_id)
    settings = request.app.state.settings
    token = await supervisor_svc.barge(
        ctx.session,
        settings,
        call,
        identity=f"supervisor-{user.id}",
        name=user.email,
        actor_user_id=user.id,
    )
    return SupervisorTokenOut(
        url=settings.livekit_public_url or settings.livekit_url,
        token=token,
        room=(call.extra or {}).get("room", ""),
    )
