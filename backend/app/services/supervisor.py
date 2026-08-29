"""P12 DR-9: supervisor ops are room/token operations, never a carrier feature. Bandwidth
conference caps (R7) never apply - this deployment never uses carrier conferences for
this. RBAC: `calls:supervise` (added by the Tier-1 schema pass, migration 0015) gates
every route in `api/routes/flows.py` that calls into this module.

whisper is NOT IMPLEMENTED (Opus B7 adjudication, verified against the LIVE LiveKit
server 2026-08-29): RoomService has no server-side subscription-permission API
(`SetSubscriptionPermissions` is a twirp 404 bad_route) - per-participant subscription
control is PUBLISHER-side, set by the supervisor's own client SDK at publish time and
enforced by the SFU there, which needs the softphone client, not this backend. See
`whisper`'s own docstring for why it raises rather than minting a token that would
silently behave like `barge` while claiming to be a whisper.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConflictError, FeatureUnavailableError
from app.models.voice import TERMINAL_CALL_STATUSES, Call
from app.models.voice import VoiceEvent as VoiceEventRow
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
    """NOT IMPLEMENTED (B7): there is no honest server-side way to enforce "the caller
    never hears the supervisor" on this LiveKit deployment. `update_subscriptions` looked
    like the right primitive, but empirically (a live probe against the deployed server)
    RoomService has no `SetSubscriptionPermissions`-equivalent that gates a participant's
    incoming tracks server-side - subscription control is set by the PUBLISHING client
    (the supervisor's own SDK) at publish time. Minting a publish token and calling
    `update_subscriptions` anyway (the previous implementation) would silently behave
    exactly like `barge` - audible to the caller - while the UI still labelled it
    "whisper". Raising is the honest behaviour until the softphone client implements
    publisher-side track visibility; still validates the call is a live room call first,
    so a bad call_id still 404s/409s rather than masking as "feature unavailable"."""
    await _active_room_and_caller_identity(session, call)
    raise FeatureUnavailableError(
        "Whisper is not available: LiveKit's RoomService has no server-side "
        "subscription-permission API (verified live: SetSubscriptionPermissions is a "
        "twirp 404 bad_route). Enforcing 'caller cannot hear the supervisor' requires "
        "publisher-side track control in the supervisor's own client SDK, which this "
        "backend does not implement."
    )


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
