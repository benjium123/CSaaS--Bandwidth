"""P6: room-per-call orchestration on top of LiveKit.

The carrier column on ``Call``/``CallLeg`` keeps naming the TRUNK carrier (telnyx today) -
LiveKit is infrastructure, never a carrier (phase-6-plan). A room-routed leg stores the
LiveKit/SIP-side id in ``provider_call_id`` (``lk-<call id>`` for a leg WE originated,
the raw SIP call id LiveKit's dispatch rule assigns for one that reached us inbound) and
``Call.extra["room"]`` names the LiveKit room, mirroring how P5 already stores everything
carrier-webhook-shaped.

P5's webhook state machine (``app.services.calls``) stays authoritative; this module reuses
its PUBLIC monotonic-rank primitives (``advance_leg`` / ``derive_call_status`` /
``load_legs`` / ``active_leg``) exactly as the Bandwidth/Telnyx webhook path does - LiveKit
events enrich the same rows through the same guards, they do not get their own parallel
state machine.

Classification note (tier-2 review B3+7): the PSTN participant in ANY room is identified by
the presence of the ``sip.callID`` attribute LiveKit-sip stamps on it - NEVER by an identity
prefix. Identities are caller-controlled data (a human's is an email-derived string; an
inbound SIP one is LiveKit's own, not ours), so a prefix check is spoofable. Room names are
opaque labels only (an inbound dispatch-rule room is ``call-_<callerNumber>_<random12>`` and
never contains the SIP call id) - the one exception is a room WE created for an outbound
call, named deterministically off our own call id, which remains a safe way to resolve it.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ConflictError, FeatureUnavailableError
from app.models import OrgNumber
from app.models.voice import TERMINAL_LEG_STATUSES, Call, CallLeg
from app.models.voice import VoiceEvent as VoiceEventRow
from app.services import calls as calls_svc
from app.voice_plane.livekit_api import (
    LiveKitApi,
    LiveKitApiError,
    mint_access_token,
    room_name_for_call,
)

if TYPE_CHECKING:
    from app.config import Settings
    from app.events.bus import EventBus

log = structlog.get_logger("voice_plane.service")

#: LiveKit's dispatch rule (deploy/livekit/README.md) names every call room "call-<id>" -
#: the suffix is OUR uuid for a room we created (outbound). An inbound room's suffix is
#: NOT parseable at all (``call-_<callerNumber>_<random12>``) - it is never used as a key,
#: only as a label. Anything not starting with this prefix is not a call room.
CALL_ROOM_PREFIX = "call-"
#: The identity WE mint for the outbound SIP participant (`create_sip_participant`). Kept
#: only so hangup/transfer can find "the participant we asked LiveKit to dial" without a
#: DB round-trip when we already have the call id in hand; it is NEVER used to classify an
#: incoming webhook event (see module docstring - that is attribute-based only).
SIP_IDENTITY_PREFIX = "sip-"

#: In-flight background outbound-dial tasks (B2), held here so nothing garbage-collects
#: them mid-flight the way an unreferenced asyncio.Task can be. Tests await
#: ``wait_for_pending_dial_tasks`` instead of sleeping/polling for one to land.
_DIAL_TASKS: set[asyncio.Task] = set()


def _spawn_dial_task(coro) -> asyncio.Task:  # noqa: ANN001
    task = asyncio.create_task(coro)
    _DIAL_TASKS.add(task)
    task.add_done_callback(_DIAL_TASKS.discard)
    return task


async def wait_for_pending_dial_tasks() -> None:
    """Test-only hook: await every in-flight background outbound-dial task deterministically
    instead of sleeping. Safe to call with nothing pending."""
    pending = list(_DIAL_TASKS)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def make_api(settings: Settings) -> LiveKitApi | None:
    """None when LiveKit is not configured - callers 503 rather than crash."""
    if not settings.livekit_url or not settings.livekit_api_secret.get_secret_value():
        return None
    return LiveKitApi(
        url=settings.livekit_url,
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret.get_secret_value(),
    )


# --------------------------------------------------------------------------------------
# Outbound: room + SIP participant
# --------------------------------------------------------------------------------------
async def start_room_call(
    session: AsyncSession,
    api: LiveKitApi,
    settings: Settings,
    bus: EventBus,
    *,
    org_id: uuid.UUID,
    to: str,
    from_e164: str,
    identity: str,
    name: str = "",
    tag: str = "",
) -> tuple[Call, CallLeg, str, str]:
    """Create the Call/CallLeg rows and the LiveKit room, then hand the SIP dial off to a
    BACKGROUND task and return immediately (tier-2 review B2).

    ``CreateSIPParticipant(wait_until_answered=True)`` blocks on LiveKit's side until the
    PSTN leg answers or LiveKit gives up - holding the HTTP request open for that would be
    wrong (a dial can legitimately ring for tens of seconds), so it never runs inline. The
    leg is only ever synchronously advanced to "dialing" here; "answered" and every dial
    failure are applied by ``_dial_and_await_answer`` once it resolves, through the SAME
    monotonic leg/call state machine every webhook uses.

    A ``LiveKitApiError`` from ``create_room`` (the one call still made inline - there is
    nothing to dial without a room) mirrors ``create_outbound_call``'s rejected branch: the
    leg/call land on "failed" with the detail recorded, and this function returns normally
    rather than raising.
    """
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="outbound",
        contact_e164=to,
        our_e164=from_e164,
        carrier="telnyx",
        status="queued",
        tag=tag or None,
    )
    room = room_name_for_call(call.id)
    call.extra = {"via": "livekit", "room": room}
    sip_identity = f"{SIP_IDENTITY_PREFIX}{call.id}"
    leg = CallLeg(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        provider_call_id=f"lk-{call.id}",
        to_e164=to,
        from_e164=from_e164,
        status="created",
        reason="original",
        extra={"sip_identity": sip_identity},
    )
    session.add(call)
    session.add(leg)
    await session.flush()

    try:
        await api.create_room(room)
    except LiveKitApiError as exc:
        leg.extra = {**(leg.extra or {}), "error_detail": str(exc)}
        calls_svc.advance_leg(leg, "failed")
        legs = await calls_svc.load_legs(session, call.id)
        calls_svc.derive_call_status(call, legs)
        await session.commit()
        await end_room_call(api, call)
    else:
        calls_svc.advance_leg(leg, "dialing")
        legs = await calls_svc.load_legs(session, call.id)
        calls_svc.derive_call_status(call, legs)
        await session.commit()

        _spawn_dial_task(
            _dial_and_await_answer(
                api=api,
                settings=settings,
                bus=bus,
                org_id=org_id,
                call_id=call.id,
                leg_id=leg.id,
                room=room,
                to=to,
                from_e164=from_e164,
                sip_identity=sip_identity,
            )
        )

    token = mint_access_token(
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret.get_secret_value(),
        identity=identity,
        name=name,
        room=room,
    )
    return call, leg, room, token


#: LiveKit's Twirp error for a dial that rang out unanswered names the reason in its
#: message text (there is no structured error code available without the SDK this project
#: deliberately does not depend on - see livekit_api.py's own docstring); anything else is
#: treated as a plain dial failure.
_NO_ANSWER_MESSAGE_MARKERS = ("timeout", "no answer", "no-answer", "ring")


def _dial_error_leg_status(exc: LiveKitApiError) -> tuple[str, str]:
    """(leg terminal status, hangup_cause) for a failed wait_until_answered dial."""
    message = str(exc).lower()
    if any(marker in message for marker in _NO_ANSWER_MESSAGE_MARKERS):
        return "hungup", "no_answer"
    return "failed", ""


async def _dial_and_await_answer(
    *,
    api: LiveKitApi,
    settings: Settings,
    bus: EventBus,
    org_id: uuid.UUID,
    call_id: uuid.UUID,
    leg_id: uuid.UUID,
    room: str,
    to: str,
    from_e164: str,
    sip_identity: str,
) -> None:
    """The background half of ``start_room_call`` (B2). Owns its OWN DB session - the
    sweeper's shape (app/services/sweeper.py), never the request's session, which is long
    gone by the time this coroutine resumes."""
    try:
        await api.create_sip_participant(
            trunk_id=settings.livekit_sip_outbound_trunk_id,
            call_to=to,
            room=room,
            from_number=from_e164,
            identity=sip_identity,
            wait_until_answered=True,
        )
    except LiveKitApiError as exc:
        terminal_status, hangup_cause = _dial_error_leg_status(exc)
        call = await _apply_dial_outcome(
            org_id=org_id,
            call_id=call_id,
            leg_id=leg_id,
            bus=bus,
            error_detail=str(exc),
            terminal_status=terminal_status,
            hangup_cause=hangup_cause,
        )
        if call is not None:
            await end_room_call(api, call)
        return
    except Exception:  # noqa: BLE001 - background task: must never crash the loop
        log.exception("livekit_dial_task_unexpected_error", call_id=str(call_id))
        return

    await _apply_dial_outcome(
        org_id=org_id, call_id=call_id, leg_id=leg_id, bus=bus, answered=True
    )


async def _apply_dial_outcome(
    *,
    org_id: uuid.UUID,
    call_id: uuid.UUID,
    leg_id: uuid.UUID,
    bus: EventBus,
    answered: bool = False,
    error_detail: str | None = None,
    terminal_status: str | None = None,
    hangup_cause: str = "",
) -> Call | None:
    from app.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        set_org_context(session, org_id)
        call = await session.get(Call, call_id)
        leg = await session.get(CallLeg, leg_id)
        if call is None or leg is None:
            return None

        changed = False
        if answered:
            changed = calls_svc.advance_leg(leg, "answered") or changed
        else:
            if error_detail is not None:
                leg.extra = {**(leg.extra or {}), "error_detail": error_detail}
            if hangup_cause:
                leg.hangup_cause = hangup_cause
            changed = calls_svc.advance_leg(leg, terminal_status or "failed") or changed

        legs = await calls_svc.load_legs(session, call.id)
        status_changed = calls_svc.derive_call_status(call, legs) or changed
        await session.commit()

        if status_changed:
            bus.publish(
                org_id, {"type": "call.status", "call_id": str(call.id), "status": call.status}
            )
        return call


async def end_room_call(api: LiveKitApi | None, call: Call) -> None:
    """Best-effort room teardown. Never raises - a delete failure must not block a hangup
    the state machine has already recorded."""
    if api is None:
        return
    room = (call.extra or {}).get("room")
    if not room:
        return
    try:
        await api.delete_room(room)
    except LiveKitApiError:
        log.warning("livekit_delete_room_failed", room=room, call_id=str(call.id))
    except Exception:  # noqa: BLE001 - best-effort by contract, never propagate
        log.exception("livekit_delete_room_unexpected_error", room=room, call_id=str(call.id))


# --------------------------------------------------------------------------------------
# Room-call control (B1): hangup / transfer. Gather is refused entirely at the route.
# --------------------------------------------------------------------------------------
async def hangup_room_call(
    session: AsyncSession, api: LiveKitApi | None, bus: EventBus, call: Call
) -> None:
    """Hang up whichever participant is carrying a room call: best-effort RemoveParticipant
    (a participant who already left is not worth surfacing as an error) then room teardown,
    then every non-terminal leg walks to "hungup" through the same monotonic guard the
    carrier webhook path uses."""
    legs = await calls_svc.load_legs(session, call.id)
    leg = calls_svc.active_leg(legs)
    sip_identity = (leg.extra or {}).get("sip_identity") if leg is not None else None
    room = (call.extra or {}).get("room")

    if api is not None and room and sip_identity:
        try:
            await api.remove_participant(room, sip_identity)
        except LiveKitApiError:
            log.warning(
                "livekit_remove_participant_failed", room=room, call_id=str(call.id)
            )

    await end_room_call(api, call)

    changed = False
    for leg_row in legs:
        if leg_row.status not in TERMINAL_LEG_STATUSES:
            changed = calls_svc.advance_leg(leg_row, "hungup") or changed
    status_changed = calls_svc.derive_call_status(call, legs) or changed
    await session.commit()
    if status_changed:
        bus.publish(
            call.org_id, {"type": "call.status", "call_id": str(call.id), "status": call.status}
        )


async def transfer_room_call(
    session: AsyncSession, api: LiveKitApi | None, bus: EventBus, call: Call, to: str
) -> None:
    """TransferSIPParticipant REFERs the PSTN leg away entirely - unlike a carrier blind
    transfer (a new leg, D6/R7), the room call simply ENDS on our side the moment the
    transfer succeeds, so no second leg is created: every leg on this call is walked
    straight to terminal and ``call.extra["transferred_to"]`` records where it went.

    Raises ``LiveKitApiError`` on failure - the route translates that into a 502."""
    if api is None:
        raise FeatureUnavailableError("LiveKit is not configured")

    legs = await calls_svc.load_legs(session, call.id)
    leg = calls_svc.active_leg(legs)
    room = (call.extra or {}).get("room")
    sip_identity = (leg.extra or {}).get("sip_identity") if leg is not None else None
    if room is None or leg is None or not sip_identity:
        raise ConflictError("This call has no active leg to transfer")

    await api.transfer_sip_participant(room=room, identity=sip_identity, transfer_to=to)

    call.extra = {**(call.extra or {}), "transferred_to": to}
    changed = False
    for leg_row in legs:
        if leg_row.status not in TERMINAL_LEG_STATUSES:
            changed = calls_svc.advance_leg(leg_row, "hungup") or changed
    status_changed = calls_svc.derive_call_status(call, legs) or changed
    await session.commit()
    if status_changed:
        bus.publish(
            call.org_id, {"type": "call.status", "call_id": str(call.id), "status": call.status}
        )


# --------------------------------------------------------------------------------------
# Webhook events
# --------------------------------------------------------------------------------------
def _deterministic_event_id(room: str, event_type: str, identity: str, created_at: object) -> str:
    """(finding 15a) LiveKit is documented to always send an event id, but when one is
    genuinely absent this is used INSTEAD of applying the event unconditionally - a
    redelivered id-less event must still dedupe, not double-apply."""
    digest = hashlib.sha256(
        f"{room}:{event_type}:{identity}:{created_at}".encode()
    ).hexdigest()
    return f"lk-{digest}"


async def _ledger_livekit_event(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    call: Call,
    leg: CallLeg | None,
    event: dict,
    room: str,
    identity: str,
) -> bool:
    """Insert one VoiceEvent row, exactly once, the same nested-savepoint shape
    ``apply_voice_event`` uses for carrier webhooks. Returns False (no-op) on a duplicate
    delivery - the DB unique constraint on (carrier, provider_event_id) is the dedupe
    mechanism, never an application-level check (D6)."""
    event_type = str(event.get("event") or "")
    created_at = event.get("createdAt")
    event_id = event.get("id") or _deterministic_event_id(room, event_type, identity, created_at)

    occurred_at = None
    if isinstance(created_at, int):
        occurred_at = datetime.fromtimestamp(created_at, tz=timezone.utc)

    voice_event = VoiceEventRow(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        leg_id=leg.id if leg is not None else None,
        carrier="livekit",
        provider_event_id=str(event_id),
        event_type=event_type,
        payload=dict(event),
        occurred_at=occurred_at,
    )
    try:
        async with session.begin_nested():
            session.add(voice_event)
            await session.flush()
    except IntegrityError:
        log.info("livekit_event_duplicate_ignored", event_id=event_id)
        await session.commit()
        return False
    return True


async def _resolve_outbound_room_leg(
    session: AsyncSession, call_id: uuid.UUID
) -> tuple[Call | None, CallLeg | None]:
    call = await session.get(Call, call_id, execution_options={ALLOW_UNSCOPED_KEY: True})
    if call is None:
        return None, None
    set_org_context(session, call.org_id)
    legs = await calls_svc.load_legs(session, call.id)
    leg = next((leg_row for leg_row in legs if leg_row.provider_call_id == f"lk-{call.id}"), None)
    if leg is None:
        leg = legs[0] if legs else None
    return call, leg


async def _resolve_inbound_room_leg(
    session: AsyncSession, sip_call_id: str
) -> tuple[Call | None, CallLeg | None]:
    """Key an inbound leg off the PSTN participant's REAL SIP call id
    (``attributes["sip.callID"]``) - never off the room name, which does not contain it."""
    if not sip_call_id:
        return None, None
    leg = (
        await session.execute(
            sa.select(CallLeg)
            .where(CallLeg.provider_call_id == sip_call_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if leg is None:
        return None, None
    set_org_context(session, leg.org_id)
    call = await session.get(Call, leg.call_id)
    return call, leg


async def _resolve_inbound_room_call_by_label(
    session: AsyncSession, room: str
) -> tuple[Call | None, CallLeg | None]:
    """Fallback for an inbound-room event that carries no participant at all (e.g.
    ``room_finished``) and so has no ``sip.callID`` to key off: the room name is still the
    opaque label we stored ourselves in ``Call.extra["room"]`` at creation time."""
    call = (
        await session.execute(
            sa.select(Call)
            .where(Call.extra["room"].as_string() == room)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if call is None:
        return None, None
    set_org_context(session, call.org_id)
    legs = await calls_svc.load_legs(session, call.id)
    return call, (legs[0] if legs else None)


async def _resolve_inbound_room_call(
    session: AsyncSession, *, room: str, sip_call_id: str
) -> tuple[Call | None, CallLeg | None]:
    if sip_call_id:
        call, leg = await _resolve_inbound_room_leg(session, sip_call_id)
        if call is not None:
            return call, leg
    return await _resolve_inbound_room_call_by_label(session, room)


async def _create_inbound_room_call(
    session: AsyncSession, *, room: str, sip_call_id: str, participant: dict
) -> tuple[Call | None, CallLeg | None, bool]:
    """A brand new inbound room: the dispatch rule (deploy/livekit/README.md) already
    created it, and the SIP participant joining is our first signal it exists. Org is
    resolved off the trunk number LiveKit's SIP attributes carry - the same discipline as
    ``webhooks.py::_resolve_voice_org``'s OrgNumber fallback, just keyed on the one number
    LiveKit hands us instead of trying both `to`/`from`. ``sip_call_id`` is the PSTN
    participant's real SIP call id (``attributes["sip.callID"]``), never derived from the
    room name (B3+7) - callers must have already refused a participant without one."""
    attributes = participant.get("attributes") or {}
    trunk_number = attributes.get("sip.trunkPhoneNumber") or ""
    contact_number = attributes.get("sip.phoneNumber") or ""

    org_number = (
        await session.execute(
            sa.select(OrgNumber)
            .where(OrgNumber.e164 == trunk_number)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if org_number is None:
        log.warning(
            "livekit_inbound_unmatched_trunk_number", room=room, trunk_number=trunk_number
        )
        return None, None, False

    org_id = org_number.org_id
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="inbound",
        contact_e164=contact_number,
        our_e164=trunk_number,
        carrier="telnyx",
        status="initiated",
        extra={"via": "livekit", "room": room},
    )
    leg = CallLeg(
        id=uuid.uuid4(),
        org_id=org_id,
        call_id=call.id,
        provider_call_id=sip_call_id,
        to_e164=trunk_number,
        from_e164=contact_number,
        status="dialing",
        reason="original",
        extra={"sip_identity": participant.get("identity") or ""},
    )
    try:
        async with session.begin_nested():
            session.add(call)
            session.add(leg)
            await session.flush()
    except IntegrityError:
        # Redelivered participant_joined raced us - not our row to own. A savepoint
        # rollback (not the whole transaction, finding 14) undoes only this insert -
        # mirrors _ledger_livekit_event's own dedupe shape above.
        log.info("livekit_inbound_leg_race_ignored", sip_call_id=sip_call_id)
        set_org_context(session, org_id)
        existing_leg = (
            await session.execute(
                sa.select(CallLeg)
                .where(CallLeg.provider_call_id == sip_call_id)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one_or_none()
        if existing_leg is None:
            raise
        existing_call = await session.get(Call, existing_leg.call_id)
        return existing_call, existing_leg, False
    return call, leg, True


async def handle_livekit_event(session: AsyncSession, bus: EventBus, event: dict) -> None:
    """Apply one LiveKit webhook event: resolve the call/leg it belongs to, ledger it
    exactly once, walk P5's monotonic leg/call state machine, then publish over the bus.

    Only rooms named ``call-*`` are ours; everything else (LiveKit room/participant chatter
    for rooms this product does not create) is silently ignored, same as an unmatched
    voice webhook is dead-lettered rather than raised.

    Answer detection (B2, the ruling): ``track_published`` is NOT an answer signal for
    ANY call - livekit-sip publishes the PSTN participant's audio track before the call is
    actually answered. An outbound room call's "answered" transition comes exclusively from
    ``start_room_call``'s background dial task resolving; this handler only ever advances a
    leg to ringing/hungup off participant presence, or finalizes every leg on room_finished.
    """
    room = ((event.get("room") or {}).get("name")) or ""
    if not room.startswith(CALL_ROOM_PREFIX):
        return
    suffix = room[len(CALL_ROOM_PREFIX) :]
    event_type = event.get("event")
    participant = event.get("participant") or {}
    identity = participant.get("identity") or ""
    attributes = participant.get("attributes") or {}
    sip_call_id = attributes.get("sip.callID") or ""
    #: The PSTN participant is whichever one carries a real SIP call id - NEVER identity
    #: prefix matching (module docstring / B3+7): identities are caller-controlled data.
    is_pstn_participant = bool(sip_call_id)

    try:
        call_uuid: uuid.UUID | None = uuid.UUID(suffix)
    except ValueError:
        call_uuid = None

    created_inbound = False
    if call_uuid is not None:
        call, leg = await _resolve_outbound_room_leg(session, call_uuid)
    else:
        call, leg = await _resolve_inbound_room_call(session, room=room, sip_call_id=sip_call_id)
        if call is None and event_type == "participant_joined" and is_pstn_participant:
            call, leg, created_inbound = await _create_inbound_room_call(
                session, room=room, sip_call_id=sip_call_id, participant=participant
            )

    if call is None:
        await session.rollback()
        return

    is_new_event = await _ledger_livekit_event(
        session, org_id=call.org_id, call=call, leg=leg, event=event, room=room, identity=identity
    )
    if not is_new_event:
        return

    if created_inbound:
        # (finding 9) publish only after the row is durable - a subscriber reacting to
        # call.ring must be able to immediately GET the call it names.
        await session.commit()
        bus.publish(
            call.org_id,
            {
                "type": "call.ring",
                "call_id": str(call.id),
                "room": room,
                "from": call.contact_e164,
                "to": call.our_e164,
            },
        )

    leg_changed = False
    if leg is not None and event_type == "participant_joined" and is_pstn_participant:
        leg_changed = calls_svc.advance_leg(leg, "ringing") or leg_changed
    elif leg is not None and event_type in (
        "participant_left",
        "participant_connection_aborted",
    ) and is_pstn_participant:
        leg_changed = calls_svc.advance_leg(leg, "hungup") or leg_changed
    elif event_type == "room_finished":
        for leg_row in await calls_svc.load_legs(session, call.id):
            leg_changed = calls_svc.advance_leg(leg_row, "hungup") or leg_changed

    legs = await calls_svc.load_legs(session, call.id)
    status_changed = calls_svc.derive_call_status(call, legs)
    await session.commit()

    if leg_changed or status_changed:
        bus.publish(
            call.org_id,
            {"type": "call.status", "call_id": str(call.id), "status": call.status},
        )
