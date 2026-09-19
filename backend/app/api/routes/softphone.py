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
from sqlalchemy.exc import SQLAlchemyError
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
from app.services import identity as identity_svc
from app.services import inbox_access as inbox_access_svc
from app.services.inbox_access import InboxAccess
from app.voice_plane.livekit_api import mint_access_token
from app.voice_plane.service import CALL_ROOM_PREFIX
from app.voice_plane.service import room_trunks as voice_plane_trunks

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
    if not voice_plane_trunks(settings):
        # (finding 12) same gate as POST /calls via="room" - a deploy with no outbound
        # trunk configured yet cannot back this feature either.
        raise FeatureUnavailableError("No LiveKit SIP outbound trunk is configured")

    call = await _call_for_room(ctx.session, payload.room)
    if call is None:
        raise NotFoundError("No call found for this room")
    # P15 (5.2): a caller with no inbox access to this call's number must not be able to
    # mint a token and join its room, no matter what they know the room name to be. Gated
    # the same way calls.py::_access_or_404 gates every by-id call route - viewer-only
    # access is not enough to JOIN a live call, and either way an inaccessible call is a
    # 404, never a 403, so existence is not leaked.
    access = await inbox_access_svc.resolve_access(
        ctx.session, ctx.actor_user_id, ctx.role.permissions or []
    )
    if not access.is_admin and not access.can_use(call.our_e164):
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
async def _ws_org_from_cookie(
    session: AsyncSession,
    settings: Settings,
    cookie: str,
    org_id: uuid.UUID,
    websocket: WebSocket,
) -> tuple[uuid.UUID, uuid.UUID, list[str]] | None:
    from datetime import datetime, timedelta, timezone

    from app.models import Session as IdentitySession
    from app.services import session_tokens

    def aware(value):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    parsed = session_tokens.parse(cookie)
    if parsed is None:
        return None
    sid, secret = parsed
    _remember_session_id(websocket, sid)
    row = await session.get(IdentitySession, sid)
    now = datetime.now(timezone.utc)
    if row is None or row.revoked_at is not None or not session_tokens.secret_matches(row, secret):
        return None
    seen = aware(row.last_seen_at or row.created_at)
    if aware(row.expires_at) <= now or now - seen > timedelta(
        minutes=settings.session_idle_minutes
    ):
        return None
    user = await users_repo.get_by_id(session, row.user_id)
    if user is None or not user.is_active:
        return None
    if settings.require_2fa_all_users and not user.has_second_factor:
        return None
    found = await orgs_repo.get_membership(session, org_id=org_id, user_id=user.id)
    if found is None:
        return None
    org, _membership, role = found
    if not await _ws_org_policy_allows(session, settings, websocket, user, org, role, row):
        return None
    return org_id, user.id, list(role.permissions or [])


async def _expire_idle_session(sid: uuid.UUID, now) -> None:  # noqa: ANN001
    """Mark an idled-out session revoked, so it is signed out everywhere and not just here.

    Takes ``now`` from the caller rather than reading the clock: this module imports
    datetime only inside functions, and a module-level ``datetime.now`` here would be a
    NameError swallowed by the except below - a fix that silently does nothing.

    Uses its own short-lived session, NOT the caller's: that one belongs to a long-lived
    websocket, and writing through it would hold a transaction open across the socket's
    network waits. Conditional on ``revoked_at IS NULL`` so a concurrent revoke wins once.
    A database failure is logged and swallowed because the caller closes the socket either
    way; a coding error is NOT swallowed.
    """
    from app.models import Session as IdentitySession

    try:
        async with get_sessionmaker()() as own:
            await own.execute(
                sa.update(IdentitySession)
                .where(IdentitySession.id == sid, IdentitySession.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            await own.commit()
    except asyncio.CancelledError:
        raise
    except SQLAlchemyError:
        log.warning("ws_idle_revoke_failed", session_id=str(sid))


async def _ws_org_policy_allows(
    session: AsyncSession,
    settings: Settings,
    websocket: WebSocket,
    user,
    org,
    role,
    session_row,
) -> bool:
    """P43: the SAME workspace rules get_current_org applies to HTTP requests - checked at
    the handshake AND on every refresh, so a removed member, a revoked session or a
    suspended workspace stops receiving events within ACCESS_TTL_SECONDS."""
    from datetime import datetime, timedelta, timezone

    from app.net import client_ip
    from app.services import passkey_policy

    def aware(value):
        return value if value is None or value.tzinfo is not None else value.replace(
            tzinfo=timezone.utc
        )

    if not org.is_active:
        return False
    now = datetime.now(timezone.utc)
    if session_row is not None:
        created = aware(session_row.created_at)
        seen = aware(session_row.last_seen_at) or created
        # The PLATFORM limits are the floor and a workspace may only tighten them. Both are
        # resolved here in one place because keeping the platform rule in the handshake
        # (_ws_org_from_cookie) and only the org rule here is exactly how an open socket came
        # to outlive the platform idle timeout by up to the absolute session lifetime.
        # A falsy org value means "unset", NOT "expire immediately".
        idle_minutes = settings.session_idle_minutes
        if org.session_idle_minutes:
            idle_minutes = min(idle_minutes, org.session_idle_minutes)
        max_hours = settings.session_max_hours
        if org.session_max_hours:
            max_hours = min(max_hours, org.session_max_hours)
        if now - seen > timedelta(minutes=idle_minutes):
            # Revoke ONLY when the platform floor is what ran out: "signed out after
            # inactivity" means signed out everywhere, exactly as auth/deps.py
            # _authenticate_cookie does. A workspace's tighter policy refuses THIS
            # workspace and must never end the account-wide session - that is why
            # _enforce_org_session_policy raises without touching revoked_at.
            if now - seen > timedelta(minutes=settings.session_idle_minutes):
                await _expire_idle_session(session_row.id, now)
            return False
        # Today this is a no-op: sessions are minted with expires_at = created +
        # SESSION_MAX_HOURS (services/identity.py) and get_live_session already enforces it.
        # It bites only after an operator LOWERS the setting, which is the right direction.
        if now - created > timedelta(hours=max_hours):
            return False
    if org.ip_allowlist and not identity_svc.ip_in_allowlist(
        client_ip(websocket), org.ip_allowlist
    ):
        return False
    if identity_svc.two_factor_required(org, user):
        return False
    if settings.require_passkey_for_privileged and passkey_policy.is_privileged_role(role):
        if not passkey_policy.session_satisfies(session_row, org):
            until = passkey_policy.grace_until(settings, user)
            if until is None or now >= until:
                return False
    return True


def _remember_session_id(websocket: WebSocket, sid) -> None:  # noqa: ANN001
    try:
        websocket._csaas_session_id = sid  # type: ignore[attr-defined]
    except AttributeError:
        pass


async def _ws_recheck(
    websocket: WebSocket,
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, list[str]] | None:
    """P43: the periodic re-check of an OPEN socket. It looks the session up by id - never by
    the cookie secret the socket was opened with, because a step-up or password change
    rotates that secret and must not sign the person out. Revocation, expiry, removal from
    the workspace and workspace policy all still end the stream."""
    sid = getattr(websocket, "_csaas_session_id", None)
    if sid is None:
        return await resolve_ws_org(websocket, session, settings)
    row = await identity_svc.get_live_session(session, sid)
    if row is None or row.user_id != user_id:
        return None
    user = await users_repo.get_by_id(session, user_id)
    if user is None or not user.is_active:
        return None
    if settings.require_2fa_all_users and not user.has_second_factor:
        return None
    found = await orgs_repo.get_membership(session, org_id=org_id, user_id=user_id)
    if found is None:
        return None
    org, _membership, role = found
    if not await _ws_org_policy_allows(session, settings, websocket, user, org, role, row):
        return None
    return org_id, user_id, list(role.permissions or [])


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
    if not org_id_raw:
        return None
    try:
        org_id = uuid.UUID(org_id_raw)
    except ValueError:
        return None

    # P42: browsers authenticate the socket with the HttpOnly session cookie. The Origin
    # must be our own console, so another site cannot ride the cookie into the socket.
    from app.services import session_tokens

    cookie = (getattr(websocket, "cookies", None) or {}).get(
        session_tokens.session_cookie_name(settings)
    )
    if cookie:
        origin = (websocket.headers.get("origin") or "").rstrip("/")
        allowed = {o.rstrip("/") for o in settings.cors_origin_list}
        allowed.add(settings.public_web_url.rstrip("/"))
        if origin not in allowed:
            return None
        return await _ws_org_from_cookie(session, settings, cookie, org_id, websocket)
    if not token or not settings.auth_bearer_compat:
        return None

    try:
        # P25: the decoder now returns (user_id, sid). The WS handshake deliberately does
        # NOT consult the revocation cache - resolve_ws_org is sync-with-the-DB already,
        # and a revoked sid is caught by get_live_session below, which is the same answer
        # the HTTP path gives without the extra cache round-trip on a one-shot handshake.
        user_id, sid = decode_access_token(token, settings.jwt_secret.get_secret_value())
    except UnauthenticatedError:
        return None

    # A revoked or expired session must not be able to open an events socket and keep it
    # open indefinitely - that would outlive "sign out everywhere" entirely.
    live_row = await identity_svc.get_live_session(session, sid) if sid is not None else None
    if sid is not None:
        _remember_session_id(websocket, sid)
    if sid is not None and live_row is None:
        return None

    user = await users_repo.get_by_id(session, user_id)
    if user is None or not user.is_active:
        return None
    # P41: the same mandatory-second-factor rule the HTTP path enforces in auth/deps.py.
    if settings.require_2fa_all_users and not user.has_second_factor:
        return None

    found = await orgs_repo.get_membership(session, org_id=org_id, user_id=user.id)
    if found is None:
        return None
    org, _membership, role = found
    if not await _ws_org_policy_allows(session, settings, websocket, user, org, role, live_row):
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
_THREAD_ID_EVENTS = frozenset({"sms.handoff", "message.received"})
#: 3.12/unknown-type hardening: event types with no per-recipient meaning that are safe
#: to broadcast to every non-admin unfiltered - the ONLY types allowed to fall through
#: the catch-all below. Anything not explicitly handled by this gate is fail-closed
#: (hidden from non-admins) rather than defaulting to visible-to-everyone, so a future
#: event type added without updating this gate does not leak by default.
_BROADCAST_EVENT_TYPES = frozenset({"ping", "appointment.booked"})


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


async def _event_visible(
    event: dict, access: InboxAccess, org_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """P15 fan-out gate, admin-first: an admin receives every event unfiltered.

    ``call.ring`` needs MEMBER access - a viewer cannot answer a call, so offering them
    the ring card would be misleading - resolved straight off the event's own ``to``
    field (voice_plane/service.py and routing_exec.py both stamp it). FAIL-CLOSED: a
    ``call.ring`` with no resolvable ``to`` is dropped for every non-admin rather than
    shown by default. 3.12: a SEQUENTIAL ring group additionally stamps ``ring_user_ids``
    naming the ONE agent actually being offered the call (routing_exec._offer_to_ring_group)
    - when present, only those user ids may see the ring at all, never every member with
    access to the number.

    ``call.status`` / ``call.handoff`` / ``call.handoff.claimed`` (by call_id) and
    ``sms.handoff`` / ``message.received`` (by thread_id) need only VIEW access, resolved
    via one DB lookup each - also fail-closed when the target row cannot be resolved.

    ``notification.created`` (P26) is the ONE type checked BEFORE the admin
    short-circuit: a bell notification belongs to exactly one person, so it goes to that
    person and nobody else. An admin holds ``inboxes:admin`` over every inbox, but that
    is access to CONVERSATIONS, never to a teammate's personal bell - without this early
    return every admin would receive every mention, assignment and missed-call entry
    addressed to someone else.

    Every other event type is FAIL-CLOSED (hidden from non-admins) unless it is in the
    explicit ``_BROADCAST_EVENT_TYPES`` allowlist (``ping`` and similar org-wide,
    no-per-recipient-meaning notifications) - a future event type this gate doesn't know
    about must never default to visible-to-everyone.
    """
    event_type = event.get("type")

    if event_type == "notification.created":
        # Fail-closed: an event with no recipient reaches nobody.
        recipient = event.get("user_id")
        return bool(recipient) and str(recipient) == str(user_id)

    if access.is_admin:
        return True

    if event_type == "call.ring":
        to = event.get("to")
        if not to:
            return False
        if to not in access.member_e164s:
            return False
        ring_user_ids = event.get("ring_user_ids")
        if ring_user_ids:
            return str(user_id) in {str(u) for u in ring_user_ids}
        return True

    if event_type in _CALL_ID_EVENTS or event_type in _THREAD_ID_EVENTS:
        e164 = await _resolve_event_e164(org_id, event)
        if not e164:
            return False
        return access.can_view(e164)

    return event_type in _BROADCAST_EVENT_TYPES


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
            # P43: re-check the session, membership and workspace rules too, not just inbox
            # grants - "sign out everywhere", removal and suspension must end the stream.
            settings: Settings = websocket.app.state.settings
            async with get_sessionmaker()() as auth_session:
                again = await _ws_recheck(websocket, auth_session, settings, org_id, user_id)
            if again is None or again[0] != org_id or again[1] != user_id:
                await websocket.close(code=4401)
                return
            permissions = again[2]
            access = await _resolve_ws_access(org_id, user_id, permissions)
            last_resolved = time.monotonic()

        if event is None:
            continue
        if not await _event_visible(event, access, org_id, user_id):
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
        if forwarder.done() and not forwarder.cancelled() and forwarder.exception() is not None:
            # P43: a failed access re-check must end the stream, not leave it open unchecked.
            log.error("events_ws_forwarder_failed", error=repr(forwarder.exception()))
            with contextlib.suppress(Exception):
                await websocket.close(code=1011)
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
