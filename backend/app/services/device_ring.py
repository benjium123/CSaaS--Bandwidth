"""Ringlite apps P3: ring the phone apps (FCM) for the same calls the web console rings.

Hooked as an EventBus tap (events/bus.py), so every present and future ring path is covered:
- ``call.ring``            -> high-priority data push ``incoming_call`` to the devices of
                              every member the console itself would ring;
- ``call.handoff.claimed`` -> ``call_cancel`` (answered elsewhere) to everyone rung;
- ``call.status``          -> ``call_cancel`` once the call is no longer ringing.

Who rings is decided by the console's own gate (routes/softphone._event_visible) evaluated
per member, so DND, ring groups, member-only access and the manager rule cannot drift apart
between web and phone. The push carries no LiveKit credential: the app answers through
``POST /calls/{id}/answer`` (atomic first-wins claim, 409 for losers), which mints it.
"""

from __future__ import annotations

import time
import uuid

import sqlalchemy as sa
import structlog

from app.config import Settings

log = structlog.get_logger(__name__)

#: A call that has not been answered or ended within this long stops being tracked.
RING_TRACK_SECONDS = 180
#: call.status values that still mean "ringing" - anything else ends the ring.
_STILL_RINGING = frozenset({"initiated", "queued", "ringing", "offered"})

#: call_id -> (org_id, user ids rung, monotonic start). In-process: the API runs one worker
#: (deploy/Dockerfile) and the bus itself is in-process for the same reason.
_ringing: dict[str, tuple[uuid.UUID, set[uuid.UUID], float]] = {}


def _prune(now: float) -> None:
    for call_id in [c for c, (_, _, t) in _ringing.items() if now - t > RING_TRACK_SECONDS]:
        _ringing.pop(call_id, None)


def make_tap(settings: Settings):
    """The EventBus tap: sync, never raises, schedules the async work."""
    from app.services import device_push, fcm

    def tap(org_id: uuid.UUID, event: dict) -> None:
        if not fcm.enabled(settings):
            return
        event_type = event.get("type")
        call_id = event.get("call_id")
        if not call_id:
            return
        if event_type == "call.ring":
            device_push.schedule(settings, ring(settings, org_id, dict(event)))
        elif event_type == "call.handoff.claimed":
            device_push.schedule(settings, cancel(settings, str(call_id), "answered_elsewhere"))
        elif event_type == "call.status" and event.get("status") not in _STILL_RINGING:
            device_push.schedule(
                settings, cancel(settings, str(call_id), str(event.get("status") or "ended"))
            )

    return tap


async def ring_recipients(org_id: uuid.UUID, event: dict) -> set[uuid.UUID]:
    """Members the console would ring for this event (the console's own gate, per member)."""
    from app.api.routes.softphone import _event_visible
    from app.db.base import set_org_context
    from app.db.session import get_sessionmaker
    from app.models import OrgMembership, Role
    from app.services import inbox_access as inbox_access_svc

    recipients: set[uuid.UUID] = set()
    async with get_sessionmaker()() as session:
        set_org_context(session, org_id)
        rows = (
            await session.execute(
                sa.select(OrgMembership.user_id, Role.permissions)
                .join(Role, Role.id == OrgMembership.role_id)
                .where(OrgMembership.org_id == org_id)
            )
        ).all()
        for user_id, permissions in rows:
            access = await inbox_access_svc.resolve_access(
                session, user_id, list(permissions or [])
            )
            if await _event_visible(event, access, org_id, user_id):
                recipients.add(user_id)
    return recipients


async def ring(settings: Settings, org_id: uuid.UUID, event: dict) -> None:
    from app.services import device_push

    call_id = str(event["call_id"])
    now = time.monotonic()
    _prune(now)
    user_ids = await ring_recipients(org_id, event)
    if not user_ids:
        return
    previous = _ringing.get(call_id)
    already = previous[1] if previous else set()
    _ringing[call_id] = (org_id, already | user_ids, previous[2] if previous else now)
    fresh = user_ids - already  # a sequential ring group re-rings: only push new people
    if not fresh:
        return
    await device_push.push_to_users(
        settings,
        fresh,
        "incoming_call",
        {
            "call_id": call_id,
            "org_id": str(org_id),
            # NOT "from": FCM reserves that data key and rejects the whole message (400), so
            # no incoming_call push was ever delivered while it was named "from".
            "caller": str(event.get("from") or ""),
            "to": str(event.get("to") or ""),
            "room": str(event.get("room") or ""),
            "sent_at": str(int(time.time())),
        },
        high_priority=True,
        collapse_key=f"call:{call_id}",
    )


async def cancel(settings: Settings, call_id: str, reason: str) -> None:
    from app.services import device_push

    entry = _ringing.pop(call_id, None)
    if entry is None:
        return
    _org_id, user_ids, _started = entry
    await device_push.push_to_users(
        settings,
        user_ids,
        "call_cancel",
        {"call_id": call_id, "reason": reason},
        high_priority=True,
        collapse_key=f"call:{call_id}",
    )


def reset() -> None:
    """Tests: forget every tracked ring."""
    _ringing.clear()
