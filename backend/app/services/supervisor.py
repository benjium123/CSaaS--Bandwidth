"""P12 DR-9: supervisor ops are room/token operations, never a carrier feature. Bandwidth
conference caps (R7) never apply - this deployment never uses carrier conferences for
this. RBAC: `calls:supervise` (added by the Tier-1 schema pass, migration 0015) gates
every route in `api/routes/flows.py` that calls into this module.

whisper (D15) is detect-and-enforce. LiveKit has no server-side SetSubscriptionPermissions
(Opus B7 adjudication, verified live 2026-08-29), so the supervisor's client sets
publisher-side track subscription permissions and this backend independently polices the
room through `LiveKitApi.update_subscriptions` from the webhook path
(`enforce_coaching_privacy`). That is the only place coaching-track policing lives; see
`whisper`'s docstring for what the SFU does and does not guarantee.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ConflictError, FeatureUnavailableError
from app.models.voice import TERMINAL_CALL_STATUSES, Call
from app.models.voice import VoiceEvent as VoiceEventRow
from app.services import audit as audit_svc
from app.services import calls as calls_svc
from app.voice_plane.livekit_api import LiveKitApi, mint_access_token

log = structlog.get_logger("supervisor")

SUPERVISOR_ACTIONS = ("monitor", "whisper", "barge")


async def _active_room_and_caller_identity(
    session: AsyncSession, call: Call
) -> tuple[str, str | None]:
    if (call.extra or {}).get("via") != "livekit":
        raise FeatureUnavailableError("Supervisor ops require a LiveKit room call")
    room = (call.extra or {}).get("room")
    if not room:
        raise ConflictError("This call has no active room")
    if call.status in TERMINAL_CALL_STATUSES:
        raise ConflictError("This call has already ended")

    legs = await calls_svc.load_legs(session, call.id)
    leg = calls_svc.active_leg(legs)
    caller_identity = (leg.extra or {}).get("sip_identity") if leg is not None else None
    return room, caller_identity


async def _record_event(
    session: AsyncSession, call: Call, action: str, actor_user_id: uuid.UUID | None
) -> None:
    session.add(
        VoiceEventRow(
            id=uuid.uuid4(),
            org_id=call.org_id,
            call_id=call.id,
            leg_id=None,
            carrier="livekit",
            provider_event_id=f"supervisor-{action}-{uuid.uuid4()}",
            event_type=f"supervisor.{action}",
            payload={"actor_user_id": str(actor_user_id) if actor_user_id else None},
        )
    )


async def monitor(
    session: AsyncSession,
    settings,  # noqa: ANN001 - app.config.Settings
    call: Call,
    *,
    identity: str,
    name: str,
    actor_user_id: uuid.UUID | None = None,
) -> str:
    """Subscribe-only token: canPublish=False. The supervisor hears the call and is never
    heard, seen, or announced to either party."""
    room, _caller_identity = await _active_room_and_caller_identity(session, call)
    token = mint_access_token(
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret.get_secret_value(),
        identity=identity,
        name=name,
        room=room,
        can_publish=False,
        can_subscribe=True,
    )
    await _record_event(session, call, "monitor", actor_user_id)
    await session.commit()
    return token


async def whisper(
    session: AsyncSession,
    settings,  # noqa: ANN001
    api: LiveKitApi | None,
    call: Call,
    *,
    identity: str,
    name: str,
    actor_user_id: uuid.UUID | None = None,
) -> str:
    """Publish token for private coaching. The supervisor's client sets publisher-side
    track subscription permissions so the customer's PSTN leg never receives the track;
    this backend's half of D15 is `enforce_coaching_privacy`, which uses
    `update_subscriptions` to prevent and revoke any PSTN subscription that gets through.
    The SFU does not expose a server-side pre-block for subscribers, so a coaching token
    is only minted when the media server connection is present to police the room.
    """
    room, _caller_identity = await _active_room_and_caller_identity(session, call)
    if api is None:
        raise FeatureUnavailableError(
            "Private coaching needs the media server connection because the backend "
            "has to be able to police the room."
        )
    token = mint_access_token(
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret.get_secret_value(),
        identity=identity,
        name=name,
        room=room,
        can_publish=True,
        can_subscribe=True,
    )

    coaching = list((call.extra or {}).get("coaching_identities") or [])
    if identity not in coaching:
        coaching.append(identity)
    call.extra = {**(call.extra or {}), "coaching_identities": coaching}
    session.add(call)

    await _record_event(session, call, "whisper", actor_user_id)
    await session.commit()
    return token


async def enforce_coaching_privacy(
    session: AsyncSession,
    api: LiveKitApi | None,
    event: dict,
) -> bool:
    """Detect-and-enforce coaching privacy from a verified LiveKit webhook event.

    Returns True when it force-unsubscribed at least one PSTN participant. LiveKit errors
    are logged and swallowed because webhook handlers must never raise.
    """
    room = ((event.get("room") or {}).get("name")) or ""
    if not room.startswith("call-"):
        return False

    event_type = event.get("event")
    if event_type not in ("track_published", "track_subscribed"):
        return False

    if api is None:
        # Nothing to police the room with. `whisper` refuses to mint a coaching token in
        # this state, so there should be no coaching track to leak - but say so out loud
        # rather than blowing up inside the webhook ack.
        log.warning("coaching_enforcement_unavailable", event_type=event_type)
        return False

    participant = event.get("participant") or {}
    publisher_identity = participant.get("identity") or ""
    if not publisher_identity:
        return False

    suffix = room[len("call-") :]
    try:
        call_uuid = uuid.UUID(suffix)
    except ValueError:
        log.info("coaching_enforcement_room_name_not_call_uuid")
        return False

    # The LiveKit webhook path carries no org header - resolve the call unscoped (exactly
    # as voice_plane/service.py's own resolvers do), then PIN the session to that call's
    # org before anything is written.
    call = await session.get(Call, call_uuid, execution_options={ALLOW_UNSCOPED_KEY: True})
    if call is None or call.status in TERMINAL_CALL_STATUSES:
        return False
    set_org_context(session, call.org_id)

    coaching = (call.extra or {}).get("coaching_identities") or []
    if publisher_identity not in coaching:
        return False

    track = event.get("track") or {}
    sid = track.get("sid") or ""
    if not sid:
        return False

    acted = False

    if event_type == "track_published":
        try:
            participants = await api.list_participants(room)
        except Exception:  # noqa: BLE001 - webhook path must not raise
            log.exception(
                "coaching_enforcement_failed",
                event_type=event_type,
                phase="list_participants",
            )
            return False

        for part in participants:
            part_identity = part.get("identity") or ""
            part_attributes = part.get("attributes") or {}
            sip_call_id = part_attributes.get("sip.callID") or ""
            if not sip_call_id or not part_identity:
                continue
            try:
                await api.update_subscriptions(
                    room=room,
                    identity=part_identity,
                    track_sids=[sid],
                    subscribe=False,
                )
                acted = True
            except Exception:  # noqa: BLE001
                log.exception(
                    "coaching_enforcement_failed",
                    event_type=event_type,
                    phase="update_subscriptions",
                )
        return acted

    # track_subscribed
    subscriber = event.get("subscriber") or {}
    subscriber_attributes = subscriber.get("attributes") or {}
    sip_call_id = subscriber_attributes.get("sip.callID") or ""
    if not sip_call_id:
        return False

    subscriber_identity = subscriber.get("identity") or ""
    if not subscriber_identity:
        return False

    try:
        await api.update_subscriptions(
            room=room,
            identity=subscriber_identity,
            track_sids=[sid],
            subscribe=False,
        )
        acted = True
    except Exception:  # noqa: BLE001
        log.exception(
            "coaching_enforcement_failed",
            event_type=event_type,
            phase="update_subscriptions",
        )

    # A leak that got through is security-relevant: ledger it and audit it even when the
    # server-side force-unsubscribe above failed.
    await _record_event(session, call, "coaching_leak", actor_user_id=None)
    audit_svc.record(
        session,
        call.org_id,
        action="supervisor.coaching_leak",
        target_type="call",
        target_id=str(call.id),
        detail={"track_sid": sid},
    )
    await session.commit()
    return acted


async def barge(
    session: AsyncSession,
    settings,  # noqa: ANN001
    call: Call,
    *,
    identity: str,
    name: str,
    actor_user_id: uuid.UUID | None = None,
) -> str:
    """Full token: the supervisor joins as a normal, audible participant."""
    room, _caller_identity = await _active_room_and_caller_identity(session, call)
    token = mint_access_token(
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret.get_secret_value(),
        identity=identity,
        name=name,
        room=room,
        can_publish=True,
        can_subscribe=True,
    )
    await _record_event(session, call, "barge", actor_user_id)
    await session.commit()
    return token
