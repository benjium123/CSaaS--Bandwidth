from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.routes.numbers import to_e164
from app.auth.deps import OrgContext, get_current_user, require_permission
from app.errors import (
    CarrierNotConfiguredError,
    ConflictError,
    FeatureUnavailableError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.models import (
    Call,
    CallLeg,
    CallRecording,
    CallTranscriptSegment,
    OrgNumber,
    QueueEntry,
    User,
)
from app.models.voice import TERMINAL_CALL_STATUSES
from app.providers.voice import as_voice_carrier
from app.services import calls as calls_svc
from app.services import inbox_access as inbox_access_svc
from app.services import recordings as recordings_svc
from app.voice_plane import service as voice_service
from app.voice_plane.livekit_api import LiveKitApiError, mint_access_token

router = APIRouter(prefix="/api/v1", tags=["calls"])

_MACHINE_DETECTION_MODES = frozenset({"off", "async"})
_VIA_MODES = frozenset({"carrier", "room"})
#: The SIP trunk configured in livekit-sip dials out on this carrier only (single trunk
#: today, findings 10/11) - a room call FROM a number on any other carrier cannot actually
#: place a call, no matter how "active" the OrgNumber row says it is.
_ROOM_TRUNK_CARRIER = "telnyx"


class CallIn(BaseModel):
    to: str = Field(min_length=3, max_length=32)
    from_: str | None = Field(default=None, alias="from", max_length=32)
    #: "At will" carrier override, honoured or refused - never silently substituted (same
    #: contract as messages.SendIn.carrier).
    carrier: str | None = Field(default=None, max_length=16)
    machine_detection: str = "off"
    tag: str = Field(default="", max_length=128)
    #: F3a: opt in to StartRecording on answer (see webhooks.py::_outbound_answer_commands).
    record: bool = False
    #: P6: "carrier" dials through the existing carrier-webhook path (P5, unchanged);
    #: "room" creates a LiveKit room + outbound SIP participant instead (voice_plane).
    via: str = "carrier"

    model_config = {"populate_by_name": True}


class TransferIn(BaseModel):
    to: str = Field(min_length=3, max_length=32)


class GatherIn(BaseModel):
    max_digits: int = Field(default=1, ge=1, le=32)
    terminating_digit: str = Field(default="#", max_length=1)
    timeout_seconds: int = Field(default=10, ge=1, le=120)
    prompt_text: str = Field(default="", max_length=500)
    action_tag: str = Field(default="", max_length=128)


class CallLegOut(BaseModel):
    id: uuid.UUID
    provider_call_id: str | None
    to_e164: str
    from_e164: str
    status: str
    reason: str
    amd_result: str | None
    answered_at: datetime | None
    ended_at: datetime | None
    hangup_cause: str | None
    created_at: datetime


class RecordingOut(BaseModel):
    id: uuid.UUID
    status: str
    content_type: str
    duration_seconds: int | None
    size_bytes: int | None
    #: OUR api url - the carrier's own URL is never exposed here or anywhere else.
    url: str | None = None


class CallOut(BaseModel):
    id: uuid.UUID
    direction: str
    contact_e164: str
    our_e164: str
    carrier: str
    status: str
    tag: str | None
    answered_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    created_at: datetime


class TranscriptSegmentOut(BaseModel):
    role: str
    text: str
    at_ms: int


class CallDetailOut(CallOut):
    legs: list[CallLegOut]
    recordings: list[RecordingOut]
    #: P8: only populated for calls the AI agent joined - None (not an empty list) when
    #: there is nothing to show, so the console's transcript panel has a single clean
    #: "no transcript" gate instead of an always-present empty array.
    transcript: list[TranscriptSegmentOut] | None = None


def _call_out(c: Call) -> CallOut:
    return CallOut(
        id=c.id,
        direction=c.direction,
        contact_e164=c.contact_e164,
        our_e164=c.our_e164,
        carrier=c.carrier,
        status=c.status,
        tag=c.tag,
        answered_at=c.answered_at,
        ended_at=c.ended_at,
        duration_seconds=c.duration_seconds,
        created_at=c.created_at,
    )


def _leg_out(leg: CallLeg) -> CallLegOut:
    return CallLegOut(
        id=leg.id,
        provider_call_id=leg.provider_call_id,
        to_e164=leg.to_e164,
        from_e164=leg.from_e164,
        status=leg.status,
        reason=leg.reason,
        amd_result=leg.amd_result,
        answered_at=leg.answered_at,
        ended_at=leg.ended_at,
        hangup_cause=leg.hangup_cause,
        created_at=leg.created_at,
    )


def _recording_out(rec: CallRecording, base_url: str, call_id: uuid.UUID) -> RecordingOut:
    url = None
    if rec.status == "stored":
        url = f"{base_url.rstrip('/')}/api/v1/calls/{call_id}/recordings/{rec.id}"
    return RecordingOut(
        id=rec.id,
        status=rec.status,
        content_type=rec.content_type,
        duration_seconds=rec.duration_seconds,
        size_bytes=rec.size_bytes,
        url=url,
    )


async def _detail_out(
    session, request: Request, call: Call, *, include_transcript: bool = True
) -> CallDetailOut:
    legs = await calls_svc.load_legs(session, call.id)
    rec_stmt = (
        sa.select(CallRecording)
        .where(CallRecording.call_id == call.id)
        .order_by(CallRecording.created_at)
    )
    recordings = list((await session.execute(rec_stmt)).scalars().all())
    transcript = None
    if include_transcript:
        # Skipped right after create_call: the flag is correct on its own merits (a call
        # that did not exist before THIS request cannot possibly have any transcript rows
        # yet, so the query would always return empty), and it ALSO happens to remove one
        # query that would otherwise race the room-call path's just-spawned background dial
        # task (B2) for the shared SQLite StaticPool connection the test suite uses - a real
        # InterfaceError, not a hypothetical one (repro: sqlite3.InterfaceError "Cursor
        # needed to be reset because of commit/rollback" from the background task's own
        # session). That race is only NARROWED by dropping this one query, not CLOSED: any
        # other concurrent query against the same pooled connection can still hit it, so
        # this comment must not be read as "the SQLite race is fixed here."
        transcript_stmt = (
            sa.select(CallTranscriptSegment)
            .where(CallTranscriptSegment.call_id == call.id)
            .order_by(CallTranscriptSegment.at_ms)
        )
        transcript_rows = list((await session.execute(transcript_stmt)).scalars().all())
        transcript = (
            [
                TranscriptSegmentOut(role=t.role, text=t.text, at_ms=t.at_ms)
                for t in transcript_rows
            ]
            if transcript_rows
            else None
        )
    base_url = request.app.state.settings.public_base_url or ""
    return CallDetailOut(
        **_call_out(call).model_dump(),
        legs=[_leg_out(leg) for leg in legs],
        recordings=[_recording_out(rec, base_url, call.id) for rec in recordings],
        transcript=transcript,
    )


async def _resolve_outbound(
    session, registry, org_id: uuid.UUID, payload: CallIn  # noqa: ANN001
) -> tuple[str, str]:
    """Resolve (carrier_name, from_e164). An explicit `from` must belong to the org and be
    active; otherwise pick any active org number sitting on a voice-capable carrier.

    F15: an explicit `carrier` is HONOURED OR REFUSED, never silently substituted for the
    number's real owner - a mismatch is a 422, not a quiet override.
    """
    if payload.from_:
        from_norm = to_e164(payload.from_)
        number = (
            await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == from_norm))
        ).scalar_one_or_none()
        if number is None or not number.is_active:
            raise ValidationFailedError(f"{from_norm} is not an active number on this org")
        if payload.carrier and payload.carrier != number.carrier:
            raise ValidationFailedError(
                f"carrier {payload.carrier!r} does not own number {number.e164!r}"
            )
        return number.carrier, number.e164

    candidates = list(
        (
            await session.execute(
                sa.select(OrgNumber).where(OrgNumber.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    for candidate in candidates:
        if payload.carrier and candidate.carrier != payload.carrier:
            continue
        carrier_name = candidate.carrier
        carrier_obj = registry.get(carrier_name) if registry is not None else None
        if carrier_obj is None:
            continue
        try:
            as_voice_carrier(carrier_obj)
        except FeatureUnavailableError:
            continue
        return carrier_name, candidate.e164

    if payload.carrier:
        raise ValidationFailedError(
            f"No active number on this org is on carrier {payload.carrier!r}"
        )
    raise ValidationFailedError("No active voice-capable number is available on this org")


async def _resolve_room_from_number(session, org_id: uuid.UUID, payload: CallIn) -> str:  # noqa: ANN001
    """via="room" number resolution (findings 10+11): NO carrier-adapter registry lookup at
    all - a LiveKit-only deploy has none registered, and requiring one would make room
    calls impossible on exactly the deployment this feature is for. An explicit `from` only
    needs to be active and org-owned; auto-picking one only ever picks a number the SIP
    trunk can actually dial out on (single trunk today: carrier == _ROOM_TRUNK_CARRIER).

    Finding 10: an explicit `from` on any OTHER carrier is refused outright (422) rather
    than silently dialed - the trunk would reject it as caller-id spoofing.
    """
    if payload.from_:
        from_norm = to_e164(payload.from_)
        number = (
            await session.execute(sa.select(OrgNumber).where(OrgNumber.e164 == from_norm))
        ).scalar_one_or_none()
        if number is None or not number.is_active:
            raise ValidationFailedError(f"{from_norm} is not an active number on this org")
        if number.carrier != _ROOM_TRUNK_CARRIER:
            raise ValidationFailedError(
                f"number {from_norm} is on {number.carrier}; the SIP trunk dials out via "
                f"{_ROOM_TRUNK_CARRIER} - pick a {_ROOM_TRUNK_CARRIER} number or add a "
                f"trunk for {number.carrier}"
            )
        return from_norm

    candidate = (
        await session.execute(
            sa.select(OrgNumber).where(
                OrgNumber.is_active.is_(True), OrgNumber.carrier == _ROOM_TRUNK_CARRIER
            )
        )
    ).scalars().first()
    if candidate is None:
        raise ValidationFailedError(
            f"No active {_ROOM_TRUNK_CARRIER} number is available on this org for a room call"
        )
    return candidate.e164


@router.post("/calls", response_model=CallDetailOut, status_code=201)
async def create_call(
    payload: CallIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:place"))],
    user: Annotated[User, Depends(get_current_user)],
) -> CallDetailOut | Response:
    if payload.machine_detection not in _MACHINE_DETECTION_MODES:
        raise ValidationFailedError("machine_detection must be 'off' or 'async'")
    if payload.via not in _VIA_MODES:
        raise ValidationFailedError("via must be 'carrier' or 'room'")

    to_norm = to_e164(payload.to)
    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )

    if payload.via == "room":
        settings = request.app.state.settings
        api = getattr(request.app.state, "livekit", None)
        if api is None:
            raise FeatureUnavailableError("LiveKit is not configured")
        if not settings.livekit_sip_outbound_trunk_id:
            # (finding 12) configured LiveKit but no outbound trunk yet - inbound-only
            # deploys are valid, but this route can never succeed without one.
            raise FeatureUnavailableError("No LiveKit SIP outbound trunk is configured")
        from_norm = await _resolve_room_from_number(ctx.session, ctx.org.id, payload)
        if not access.can_use(from_norm):
            raise PermissionDeniedError(f"You do not have call access to {from_norm}")
        bus = request.app.state.event_bus
        call, _leg, room, token = await voice_service.start_room_call(
            ctx.session,
            api,
            settings,
            bus,
            org_id=ctx.org.id,
            to=to_norm,
            from_e164=from_norm,
            identity=f"user-{user.id}",
            name=user.email,
            tag=payload.tag,
        )
        detail = await _detail_out(ctx.session, request, call, include_transcript=False)
        body = detail.model_dump(mode="json")
        url = settings.livekit_public_url or settings.livekit_url
        body.update({"room": room, "token": token, "url": url})
        return JSONResponse(status_code=201, content=body)

    registry = getattr(request.app.state, "carriers", None)
    _carrier_name, from_norm = await _resolve_outbound(ctx.session, registry, ctx.org.id, payload)
    if not access.can_use(from_norm):
        raise PermissionDeniedError(f"You do not have call access to {from_norm}")
    call, _leg = await calls_svc.create_outbound_call(
        ctx.session,
        registry,
        ctx.org.id,
        to=to_norm,
        from_=from_norm,
        carrier_name=_carrier_name,
        machine_detection=payload.machine_detection,
        tag=payload.tag,
        record=payload.record,
    )
    # A call this request just created cannot have any transcript rows yet.
    return await _detail_out(ctx.session, request, call, include_transcript=False)


@router.get("/calls", response_model=list[CallOut])
async def list_calls(
    ctx: Annotated[OrgContext, Depends(require_permission("calls:read"))],
    contact_e164: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[CallOut]:
    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    stmt = sa.select(Call).order_by(Call.created_at.desc()).limit(limit).offset(offset)
    if contact_e164:
        stmt = stmt.where(Call.contact_e164 == contact_e164)
    if status:
        stmt = stmt.where(Call.status == status)
    if not access.is_admin:
        stmt = stmt.where(Call.our_e164.in_(access.member_e164s | access.viewer_e164s))
    rows = (await ctx.session.execute(stmt)).scalars().all()
    return [_call_out(c) for c in rows]


async def _access_or_404(
    ctx: OrgContext, call: Call, *, require_use: bool, message: str = "Call not found"
) -> None:
    """P15: gate a by-id call route the same way get_call does. An inaccessible call is a
    404, never a 403 - and this must run BEFORE any other check on the route (a room/
    status ConflictError, a recording lookup, ...) that could otherwise leak the call's
    existence to a caller with no access to it at all.
    """
    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    if access.is_admin:
        return
    ok = access.can_use(call.our_e164) if require_use else access.can_view(call.our_e164)
    if not ok:
        raise NotFoundError(message)


@router.get("/calls/{call_id}", response_model=CallDetailOut)
async def get_call(
    call_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:read"))],
) -> CallDetailOut:
    call = await ctx.session.get(Call, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    if not access.is_admin and not access.can_view(call.our_e164):
        # An inaccessible call's detail is a 404, never a 403 - don't leak existence.
        raise NotFoundError("Call not found")
    return await _detail_out(ctx.session, request, call)


class SoftphoneAnswerOut(BaseModel):
    url: str
    token: str
    room: str


@router.post("/calls/{call_id}/answer", response_model=SoftphoneAnswerOut)
async def answer_call(
    call_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:place"))],
    user: Annotated[User, Depends(get_current_user)],
) -> SoftphoneAnswerOut:
    """The browser softphone's counterpart to /softphone/token for an INBOUND room call:
    same token payload, but reached from the call itself (the UI has a ringing Call, not a
    room name) and gated on the call still being live."""
    call = await ctx.session.get(Call, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    await _access_or_404(ctx, call, require_use=True)

    # Pre-existing bug: `call.extra.get(...)` dereferences None when `extra` itself is
    # None (e.g. a carrier-path call with no `extra` set at all) - every sibling check
    # in this file guards with `(call.extra or {})` first.
    room = (call.extra or {}).get("room") if (call.extra or {}).get("via") == "livekit" else None
    if room is None:
        raise ConflictError("This call is not a LiveKit room call")
    if call.status in TERMINAL_CALL_STATUSES:
        raise ConflictError("This call has already ended")

    settings = request.app.state.settings
    token = mint_access_token(
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret.get_secret_value(),
        identity=f"user-{user.id}",
        name=user.email,
        room=room,
    )
    # F9: tell every OTHER operator's softphone this ring/handoff card is already
    # claimed so it disappears from their incoming list too - cheap and harmless to
    # publish unconditionally for every room-call answer (we already know this is a
    # room call from the check above).
    bus = request.app.state.event_bus
    bus.publish(
        call.org_id,
        {"type": "call.handoff.claimed", "call_id": str(call.id)},
    )
    # P12 (Opus B12): a queued room call answered through the normal console must also
    # resolve its QueueEntry, or the routing tick overflows it to voicemail mid-talk.
    # Conditional UPDATE = first-answer-wins; losing nothing when the call was never
    # queued (rowcount 0 is fine).
    await ctx.session.execute(
        sa.update(QueueEntry)
        .where(
            QueueEntry.call_id == call.id,
            QueueEntry.state.in_(("waiting", "offered")),
        )
        .values(
            state="connected", offered_user_id=user.id, resolved_at=datetime.now(timezone.utc)
        )
    )
    await ctx.session.commit()
    return SoftphoneAnswerOut(
        url=settings.livekit_public_url or settings.livekit_url, token=token, room=room
    )


@router.post("/calls/{call_id}/transfer", response_model=CallDetailOut)
async def transfer_call(
    call_id: uuid.UUID,
    payload: TransferIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:place"))],
) -> CallDetailOut | Response:
    call = await ctx.session.get(Call, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    await _access_or_404(ctx, call, require_use=True)

    to_norm = to_e164(payload.to)

    if (call.extra or {}).get("via") == "livekit":
        api = getattr(request.app.state, "livekit", None)
        bus = request.app.state.event_bus
        try:
            await voice_service.transfer_room_call(ctx.session, api, bus, call, to_norm)
        except LiveKitApiError as exc:
            return JSONResponse(
                status_code=502,
                content={"error": {"code": "livekit_transfer_failed", "message": str(exc)}},
            )
        return await _detail_out(ctx.session, request, call)

    registry = getattr(request.app.state, "carriers", None)
    try:
        await calls_svc.start_blind_transfer(ctx.session, registry, call, to_norm)
    except FeatureUnavailableError as exc:
        raise ValidationFailedError(str(exc)) from exc
    except ConflictError:
        raise
    return await _detail_out(ctx.session, request, call)


class AgentDispatchIn(BaseModel):
    agent_name: str = "echo"


@router.post("/calls/{call_id}/agent")
async def dispatch_agent(
    call_id: uuid.UUID,
    payload: AgentDispatchIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:place"))],
) -> dict:
    """Dispatch a named agent worker (P7 echo agent, P8 AI agent) into a live room call.

    Room calls only: an agent is a room participant, and carrier-path calls have no room
    for it to join.
    """
    call = await ctx.session.get(Call, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    await _access_or_404(ctx, call, require_use=True)
    extra = call.extra or {}
    if extra.get("via") != "livekit":
        raise ConflictError("Agents can only join room calls (via=room)")
    if call.status in TERMINAL_CALL_STATUSES:
        raise ConflictError(f"Call is already {call.status}")
    api = getattr(request.app.state, "livekit", None)
    if api is None:
        raise CarrierNotConfiguredError("LiveKit is not configured on this deployment")
    result = await api.create_agent_dispatch(
        room=extra["room"], agent_name=payload.agent_name, metadata=str(call.id)
    )
    return {"dispatched": payload.agent_name, "room": extra["room"], "id": result.get("id", "")}


@router.post("/calls/{call_id}/hangup", response_model=CallDetailOut)
async def hangup_call(
    call_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:place"))],
) -> CallDetailOut:
    call = await ctx.session.get(Call, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    await _access_or_404(ctx, call, require_use=True)

    if (call.extra or {}).get("via") == "livekit":
        api = getattr(request.app.state, "livekit", None)
        bus = request.app.state.event_bus
        await voice_service.hangup_room_call(ctx.session, api, bus, call)
        return await _detail_out(ctx.session, request, call)

    registry = getattr(request.app.state, "carriers", None)
    try:
        await calls_svc.hangup_active_leg(ctx.session, registry, call)
    except FeatureUnavailableError as exc:
        raise ValidationFailedError(str(exc)) from exc
    return await _detail_out(ctx.session, request, call)


@router.post("/calls/{call_id}/gather", response_model=CallDetailOut)
async def gather_call(
    call_id: uuid.UUID,
    payload: GatherIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:place"))],
) -> CallDetailOut:
    call = await ctx.session.get(Call, call_id)
    if call is None:
        raise NotFoundError("Call not found")
    await _access_or_404(ctx, call, require_use=True)

    if (call.extra or {}).get("via") == "livekit":
        raise ConflictError(
            "Room-call DTMF is sent by the browser directly; server-side gather is "
            "carrier-path only"
        )

    registry = getattr(request.app.state, "carriers", None)
    try:
        await calls_svc.start_gather(
            ctx.session,
            registry,
            call,
            max_digits=payload.max_digits,
            terminating_digit=payload.terminating_digit,
            timeout_seconds=payload.timeout_seconds,
            prompt_text=payload.prompt_text,
            action_tag=payload.action_tag,
        )
    except FeatureUnavailableError as exc:
        raise ValidationFailedError(str(exc)) from exc
    except ConflictError:
        raise
    return await _detail_out(ctx.session, request, call)


@router.get("/calls/{call_id}/recordings/{recording_id}")
async def get_recording(
    call_id: uuid.UUID,
    recording_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:read"))],
) -> Response:
    recording = await ctx.session.get(CallRecording, recording_id)
    if recording is None or recording.call_id != call_id:
        raise NotFoundError("Recording not found")
    call = await ctx.session.get(Call, call_id)
    if call is None:
        raise NotFoundError("Recording not found")
    await _access_or_404(ctx, call, require_use=False, message="Recording not found")
    if recording.status != "stored" or not recording.storage_key:
        raise NotFoundError("Recording not found")

    store = getattr(request.app.state, "media_store", None)
    if store is None:  # pragma: no cover - lifespan always sets it
        raise ValidationFailedError("Media storage is not configured")

    try:
        data = await recordings_svc.load_recording_bytes(store, recording)
    except KeyError as exc:
        raise NotFoundError("Recording not found") from exc

    return Response(
        content=data,
        media_type=recording.content_type or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=900"},
    )
