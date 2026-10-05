"""Bring a teammate into a live softphone (LiveKit room) call: add them (3-way) or hand the
call over (transfer). Transfers never leave the workspace: the only targets are teammates
who may use the call's number and the workspace's own active numbers.

An invite rings ONLY the invited person (a personal ``call.invite`` event, never a
``call.ring`` that admins would also see) and lives in ``call.extra["invites"]`` for
INVITE_TTL seconds. A transfer puts the caller on hold (music) until the teammate joins.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog

from app.errors import ConflictError, FeatureUnavailableError, ValidationFailedError
from app.models import WILDCARD, OrgMembership, Role, User
from app.models.voice import TERMINAL_CALL_STATUSES
from app.services import inbox_access as inbox_access_svc

log = structlog.get_logger("call_invites")

INVITE_TTL = timedelta(seconds=60)
MODES = ("add", "transfer")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _require_room_call(call) -> str:  # noqa: ANN001
    extra = call.extra or {}
    room = extra.get("room")
    if extra.get("via") != "livekit" or not room:
        raise ConflictError("Only calls made or answered in the app can include a teammate")
    if call.status in TERMINAL_CALL_STATUSES:
        raise ConflictError("This call has ended")
    return room


async def teammates_for(session, call, *, exclude_user_id: uuid.UUID | None) -> list[dict]:  # noqa: ANN001
    """Teammates who may place calls AND use the call's number (current org context)."""
    rows = (
        await session.execute(
            sa.select(OrgMembership, User, Role)
            .join(User, User.id == OrgMembership.user_id)
            .join(Role, Role.id == OrgMembership.role_id)
        )
    ).all()
    out: list[dict] = []
    for _m, user, role in rows:
        if exclude_user_id is not None and user.id == exclude_user_id:
            continue
        permissions = role.permissions or []
        if "calls:place" not in permissions and WILDCARD not in permissions:
            continue
        access = await inbox_access_svc.resolve_access(session, user.id, permissions)
        if not access.can_use(call.our_e164):
            continue
        out.append(
            {"user_id": str(user.id), "name": user.full_name or user.email, "email": user.email}
        )
    out.sort(key=lambda t: t["name"].casefold())
    return out


def _live_invites(call) -> dict:  # noqa: ANN001
    now = _now()
    invites = dict((call.extra or {}).get("invites") or {})
    for uid, inv in list(invites.items()):
        try:
            at = datetime.fromisoformat(inv["at"])
        except (KeyError, TypeError, ValueError):
            invites.pop(uid)
            continue
        if now - at > INVITE_TTL:
            invites.pop(uid)
    return invites


async def invite(
    session, bus, api, settings, call, *, inviter: User, target_user_id: uuid.UUID, mode: str  # noqa: ANN001
) -> None:
    """Ring one teammate into the call. Transfer mode also holds the caller (music)."""
    room = _require_room_call(call)
    if mode not in MODES:
        raise ValidationFailedError("Mode must be add or transfer")
    if target_user_id == inviter.id:
        raise ValidationFailedError("You are already on this call")
    targets = await teammates_for(session, call, exclude_user_id=inviter.id)
    if str(target_user_id) not in {t["user_id"] for t in targets}:
        raise ValidationFailedError("That teammate cannot take calls on this number")

    extra = dict(call.extra or {})
    invites = _live_invites(call)
    invites[str(target_user_id)] = {"mode": mode, "by": str(inviter.id), "at": _now().isoformat()}
    extra["invites"] = invites
    call.extra = extra

    if mode == "transfer":
        from app.services import call_hold

        if not (call.extra or {}).get("on_hold"):
            try:
                await call_hold.set_hold(session, api, settings, call, on=True)
                call.extra = {**(call.extra or {}), "transfer_hold": str(target_user_id)}
            except FeatureUnavailableError:
                log.info("transfer_without_hold_music", call_id=str(call.id))
    await session.commit()

    by_name = inviter.full_name or inviter.email
    bus.publish(
        call.org_id,
        {
            "type": "call.invite",
            "user_id": str(target_user_id),
            "call_id": str(call.id),
            "room": room,
            "mode": mode,
            "by": by_name,
            "from": call.contact_e164,
            "to": call.our_e164,
        },
    )


async def _clear(session, api, settings, call, user_id: str) -> dict | None:  # noqa: ANN001
    """Drop one invite; resume the caller if that invite's transfer put them on hold."""
    extra = dict(call.extra or {})
    invites = dict(extra.get("invites") or {})
    found = invites.pop(user_id, None)
    extra["invites"] = invites
    resume = extra.get("transfer_hold") == user_id
    if resume:
        extra.pop("transfer_hold", None)
    call.extra = extra
    if resume and extra.get("on_hold"):
        from app.services import call_hold

        try:
            await call_hold.set_hold(session, api, settings, call, on=False)
        except (ConflictError, FeatureUnavailableError):
            pass
    return found


async def accept(session, bus, api, settings, call, *, user: User) -> str:  # noqa: ANN001
    """The invited teammate joins. Returns the room name; raises if there is no live invite."""
    room = _require_room_call(call)
    live = _live_invites(call)
    inv = live.get(str(user.id))
    if inv is None:
        raise ConflictError("This invitation has expired or was cancelled")
    await _clear(session, api, settings, call, str(user.id))
    await session.commit()
    bus.publish(
        call.org_id,
        {
            "type": "call.invite.update",
            "user_id": inv["by"],
            "call_id": str(call.id),
            "invitee": str(user.id),
            "invitee_name": user.full_name or user.email,
            "mode": inv["mode"],
            "state": "accepted",
        },
    )
    return room


async def cancel(session, bus, api, settings, call, *, actor: User, user_id: uuid.UUID) -> None:  # noqa: ANN001
    """The inviter cancels, or the invitee declines. Unknown/expired invites are a no-op."""
    inv = await _clear(session, api, settings, call, str(user_id))
    await session.commit()
    if inv is None:
        return
    declined = actor.id == user_id
    notify = inv["by"] if declined else str(user_id)
    bus.publish(
        call.org_id,
        {
            "type": "call.invite.update",
            "user_id": notify,
            "call_id": str(call.id),
            "invitee": str(user_id),
            "mode": inv["mode"],
            "state": "declined" if declined else "cancelled",
        },
    )


async def require_own_number(session, org_id: uuid.UUID, e164: str) -> None:  # noqa: ANN001
    """Transfers never go outside the workspace: only its own active numbers."""
    from app.services import calls as calls_svc

    try:
        await calls_svc.require_owned_caller_ids(session, org_id, [e164])
    except ValidationFailedError as exc:
        raise ValidationFailedError(
            "Calls can only be transferred to your teammates or your own numbers"
        ) from exc
