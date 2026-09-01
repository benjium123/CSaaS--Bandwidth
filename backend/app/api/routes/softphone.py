"""P6: browser softphone endpoints - a room-scoped LiveKit token, and the realtime events
websocket that rings the org when an inbound room call arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import OrgContext, get_current_user, require_permission
from app.auth.security import decode_access_token
from app.config import Settings
from app.db.base import set_org_context
from app.db.session import get_sessionmaker
from app.errors import ConflictError, FeatureUnavailableError, NotFoundError, UnauthenticatedError
from app.events.bus import EventBus
from app.models import Call, CallLeg, MessageThread, User
from app.models.voice import TERMINAL_CALL_STATUSES
from app.repositories import orgs as orgs_repo
from app.repositories import users as users_repo
from app.services import inbox_access as inbox_access_svc
from app.services.inbox_access import InboxAccess
from app.voice_plane.livekit_api import mint_access_token
from app.voice_plane.service import CALL_ROOM_PREFIX

router = APIRouter(tags=["softphone"])
log = structlog.get_logger("softphone")

#: How often the WS sends a keepalive frame while nothing else is happening. Also caps how
#: long a stalled `queue.get()` blocks before we check the connection is still worth serving.
PING_INTERVAL_SECONDS = 25

#: P15: how long a resolved InboxAccess is trusted inside one WS connection before it is
#: re-resolved from the DB. Without this, revoking a grant has no effect on an already-open
#: socket until the client reconnects.
ACCESS_TTL_SECONDS = 60


class SoftphoneTokenIn(BaseModel):
    room: str = Field(min_length=1, max_length=128)


class SoftphoneTokenOut(BaseModel):
    url: str
    token: str
    room: str


async def _call_for_room(session: AsyncSession, room: str) -> Call | None:
    """Resolve a LiveKit room name back to the (tenant-scoped) Call it belongs to - the
    inverse of ``room_name_for_call`` for an outbound room, or a CallLeg.provider_call_id
    lookup for an inbound one (see voice_plane/service.py's own resolution for why the two
    shapes differ). (B6) Never resolves a call that isn't actually a LiveKit room call -
    ``via`` must say so, not merely a room-shaped name."""
    if not room.startswith(CALL_ROOM_PREFIX):
        return None
    suffix = room[len(CALL_ROOM_PREFIX) :]
    try:
        call_id = uuid.UUID(suffix)
    except ValueError:
        call_id = None

    if call_id is not None:
        call = await session.get(Call, call_id)
    else:
        leg = (
            await session.execute(sa.select(CallLeg).where(CallLeg.provider_call_id == suffix))
        ).scalar_one_or_none()
        call = await session.get(Call, leg.call_id) if leg is not None else None

    if call is None or (call.extra or {}).get("via") != "livekit":
        return None
    return call


@router.post("/api/v1/softphone/token", response_model=SoftphoneTokenOut)
async def softphone_token(
    payload: SoftphoneTokenIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:place"))],
    user: Annotated[User, Depends(get_current_user)],
) -> SoftphoneTokenOut:
    settings: Settings = request.app.state.settings
    if getattr(request.app.state, "livekit", None) is None:
        raise FeatureUnavailableError("LiveKit is not configured")
    if not settings.livekit_sip_outbound_trunk_id:
        # (finding 12) same gate as POST /calls via="room" - a deploy with no outbound
        # trunk configured yet cannot back this feature either.
        raise FeatureUnavailableError("No LiveKit SIP outbound trunk is configured")

    call = await _call_for_room(ctx.session, payload.room)
    if call is None:
        raise NotFoundError("No call found for this room")
    if call.status in TERMINAL_CALL_STATUSES:
        raise ConflictError("This call has already ended")

    token = mint_access_token(
        api_key=settings.livekit_api_key,
        api_secret=settings.livekit_api_secret.get_secret_value(),
        identity=f"user-{user.id}",
        name=user.email,
        room=payload.room,
    )
    return SoftphoneTokenOut(
        url=settings.livekit_public_url or settings.livekit_url,
        token=token,
        room=payload.room,
    )


# --------------------------------------------------------------------------------------
# Realtime events websocket
# --------------------------------------------------------------------------------------
async def resolve_ws_org(
    websocket: WebSocket, session: AsyncSession, settings: Settings
) -> tuple[uuid.UUID, uuid.UUID, list[str]] | None:
    """Query-param auth for the WS handshake: a browser cannot set headers on a WS upgrade
    request, so token + org travel as ``?token=...&org_id=...`` instead of the
    Authorization/X-Org-Id headers every HTTP route uses. Verified with the SAME JWT
    decoder and the SAME membership check the HTTP path uses (get_current_user /
    get_current_org) - only the transport for the credentials differs.

    Returns ``(org_id, user_id, permissions)`` - the permission list travels along so the
    caller can resolve P15 inbox access (ring fan-out) without a second membership lookup.
    """
    token = websocket.query_params.get("token")
    org_id_raw = websocket.query_params.get("org_id")
    if not token or not org_id_raw:
        return None
    try:
        org_id = uuid.UUID(org_id_raw)
    except ValueError:
        return None

    try:
        user_id = decode_access_token(token, settings.jwt_secret.get_secret_value())
    except UnauthenticatedError:
        return None

    user = await users_repo.get_by_id(session, user_id)
    if user is None or not user.is_active:
        return None

    found = await orgs_repo.get_membership(session, org_id=org_id, user_id=user.id)
    if found is None:
        return None
    org, _membership, role = found
    if not org.is_active:
        return None
    return org_id, user.id, list(role.permissions or [])


async def _watch_disconnect(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return


#: Event types P15 gates by resolving an our_e164 for them. ``call.ring`` is handled
#: separately (member-only, resolved straight off the event's own "to" - no DB hit); every
#: type here needs a DB lookup to find the number it belongs to.
_CALL_ID_EVENTS = frozenset(
    {"call.status", "call.handoff", "call.handoff.claimed", "queue.callback_requested"}
)
_THREAD_ID_EVENTS = frozenset({"sms.handoff"})


async def _resolve_ws_access(
    org_id: uuid.UUID, user_id: uuid.UUID, permissions: list[str]
) -> InboxAccess:
    async with get_sessionmaker()() as session:
        set_org_context(session, org_id)
        return await inbox_access_svc.resolve_access(session, user_id, permissions)


async def _resolve_event_e164(org_id: uuid.UUID, event: dict) -> str | None:
    """Resolve the our_e164 an event belongs to, for the gates below. A short-lived
    session per lookup - same reasoning as the auth-only session in events_ws: the WS loop
    never holds a DB connection open across the whole (possibly hours-long) connection."""
    event_type = event.get("type")
    async with get_sessionmaker()() as session:
        set_org_context(session, org_id)
        if event_type in _CALL_ID_EVENTS:
            raw = event.get("call_id")
            if not raw:
                return None
            try:
                call = await session.get(Call, uuid.UUID(raw))
            except ValueError:
                return None
            return call.our_e164 if call is not None else None
        if event_type in _THREAD_ID_EVENTS:
            raw = event.get("thread_id")
            if not raw:
                return None
            try:
                thread = await session.get(MessageThread, uuid.UUID(raw))
            except ValueError:
                return None
            return thread.our_e164 if thread is not None else None
    return None


async def _event_visible(event: dict, access: InboxAccess, org_id: uuid.UUID) -> bool:
    """P15 fan-out gate, admin-first: an admin receives every event unfiltered.

    ``call.ring`` needs MEMBER access - a viewer cannot answer a call, so offering them
    the ring card would be misleading - resolved straight off the event's own ``to``
    field (voice_plane/service.py and routing_exec.py both stamp it). FAIL-CLOSED: a
    ``call.ring`` with no resolvable ``to`` is dropped for every non-admin rather than
    shown by default.

    ``call.status`` / ``call.handoff`` / ``call.handoff.claimed`` (by call_id) and
    ``sms.handoff`` (by thread_id) need only VIEW access, resolved via one DB lookup
    each - also fail-closed when the target row cannot be resolved.

    Every other event type (``ping``, and any future shape this gate doesn't know about)
    passes through unfiltered, exactly as before P15.
    """
    if access.is_admin:
        return True

    event_type = event.get("type")

    if event_type == "call.ring":
        to = event.get("to")
        if not to:
            return False
        return to in access.member_e164s

    if event_type in _CALL_ID_EVENTS or event_type in _THREAD_ID_EVENTS:
        e164 = await _resolve_event_e164(org_id, event)
        if not e164:
            return False
        return access.can_view(e164)

    return True


async def _forward_events(
    websocket: WebSocket,
    queue: asyncio.Queue,
    access: InboxAccess,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    permissions: list[str],
) -> None:
    last_resolved = time.monotonic()
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            event = None
            await websocket.send_json({"type": "ping"})

        # P15: re-resolve on a TTL so a grant revoked mid-connection stops being honoured
        # without the client having to reconnect. Checked every loop iteration (including
        # ping timeouts, every PING_INTERVAL_SECONDS) so the TTL is enforced with
        # reasonable granularity even on a quiet connection.
        if time.monotonic() - last_resolved >= ACCESS_TTL_SECONDS:
            access = await _resolve_ws_access(org_id, user_id, permissions)
            last_resolved = time.monotonic()

        if event is None:
            continue
        if not await _event_visible(event, access, org_id):
            continue
        await websocket.send_json(event)


async def pump_events(
    websocket: WebSocket,
    queue: asyncio.Queue,
    access: InboxAccess,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    permissions: list[str],
) -> None:
    """Forward bus events to the socket until either side goes away."""
    watcher = asyncio.ensure_future(_watch_disconnect(websocket))
    forwarder = asyncio.ensure_future(
        _forward_events(
            websocket, queue, access, org_id=org_id, user_id=user_id, permissions=permissions
        )
    )
    try:
        await asyncio.wait({watcher, forwarder}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (watcher, forwarder):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


@router.websocket("/api/v1/events/ws")
async def events_ws(websocket: WebSocket) -> None:
    settings: Settings = websocket.app.state.settings

    # A short-lived session just for the auth check - NOT Depends(get_session), which
    # would hold a pooled DB connection open for the whole (possibly hours-long) websocket
    # lifetime instead of only for the handshake. P15 inbox access is resolved in the same
    # session/context, so the initial ring-fan-out gate costs no extra connection (the
    # periodic TTL re-resolve in _forward_events opens its own short-lived sessions).
    async with get_sessionmaker()() as session:
        resolved = await resolve_ws_org(websocket, session, settings)
        access: InboxAccess | None = None
        if resolved is not None:
            org_id, user_id, permissions = resolved
            set_org_context(session, org_id)
            access = await inbox_access_svc.resolve_access(session, user_id, permissions)

    if resolved is None:
        # Starlette requires accept() before a websocket can be closed with a reason code.
        await websocket.accept()
        await websocket.close(code=4401)
        return

    await websocket.accept()
    bus: EventBus = websocket.app.state.event_bus
    async with bus.subscribe(org_id) as queue:
        await pump_events(
            websocket, queue, access, org_id=org_id, user_id=user_id, permissions=permissions
        )
