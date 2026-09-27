"""Park a live softphone (LiveKit room) call and let any teammate on its number pick it up.

Park = the caller goes on hold (music) and the agent leaves the room; the call stays live
(only the caller's own leg ending ends it). ``call.extra["parked"]`` marks it and a
``call.parked`` event goes to everyone who can take calls on the number. Pickup clears the
mark, takes the caller off hold and hands back the room - the same join as an invite.
Parking never sends a call outside the workspace.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog

from app.errors import ConflictError, FeatureUnavailableError
from app.models import Call, User
from app.models.voice import TERMINAL_CALL_STATUSES
from app.services.call_invites import _require_room_call

log = structlog.get_logger("call_park")

#: Parked calls older than this are not listed (MAX_CALL_SECONDS cuts them off anyway).
LIST_WINDOW = timedelta(hours=4)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _name(user: User) -> str:
    return user.full_name or user.email


async def park(session, bus, api, settings, call, *, user: User) -> None:  # noqa: ANN001
    """Hold the caller with music and mark the call parked. The agent then leaves."""
    from app.services import call_hold

    room = _require_room_call(call)
    if (call.extra or {}).get("parked"):
        raise ConflictError("This call is already parked")
    if not (call.extra or {}).get("on_hold"):
        try:
            await call_hold.set_hold(session, api, settings, call, on=True)
        except FeatureUnavailableError as exc:
            # Never leave a caller alone in silence.
            raise ConflictError("Parking needs hold music, which is not available") from exc
    at = _now().isoformat()
    call.extra = {
        **(call.extra or {}),
        "parked": {"by": str(user.id), "by_name": _name(user), "at": at},
    }
    await session.commit()
    log.info("call_parked", call_id=str(call.id), by=str(user.id))
    bus.publish(
        call.org_id,
        {
            "type": "call.parked",
            "call_id": str(call.id),
            "room": room,
            "from": call.contact_e164,
            "to": call.our_e164,
            "by": _name(user),
            "at": at,
            "state": "parked",
        },
    )


async def pickup(session, bus, api, settings, call, *, user: User) -> str:  # noqa: ANN001
    """Take a parked call: clears the mark, resumes the caller, returns the room name."""
    from app.services import call_hold

    await session.refresh(call, with_for_update=True)  # two pickups: only one wins
    room = _require_room_call(call)
    if not (call.extra or {}).get("parked"):
        raise ConflictError("This call is no longer parked")
    extra = dict(call.extra or {})
    extra.pop("parked", None)
    call.extra = extra
    if extra.get("on_hold"):
        try:
            await call_hold.set_hold(session, api, settings, call, on=False)
        except (ConflictError, FeatureUnavailableError):
            pass
    await session.commit()
    log.info("call_picked_up", call_id=str(call.id), by=str(user.id))
    bus.publish(
        call.org_id,
        {
            "type": "call.parked",
            "call_id": str(call.id),
            "room": room,
            "from": call.contact_e164,
            "to": call.our_e164,
            "by": _name(user),
            "state": "picked_up",
        },
    )
    return room


async def list_parked(session, access) -> list[dict]:  # noqa: ANN001
    """Live parked calls on numbers this person may take calls on (org context set)."""
    since = _now() - LIST_WINDOW
    if session.get_bind().dialect.name == "sqlite":
        since = since.replace(tzinfo=None)
    rows = (
        await session.execute(
            sa.select(Call)
            .where(
                Call.status.not_in(tuple(TERMINAL_CALL_STATUSES)),
                Call.created_at >= since,
            )
            .order_by(Call.created_at)
        )
    ).scalars()
    out: list[dict] = []
    for call in rows:
        parked = (call.extra or {}).get("parked")
        if not parked or not access.can_use(call.our_e164):
            continue
        out.append(
            {
                "call_id": str(call.id),
                "from": call.contact_e164,
                "to": call.our_e164,
                "by": parked.get("by_name"),
                "at": parked.get("at"),
            }
        )
    return out

