"""Hold with music for softphone (LiveKit room) calls.

On hold the caller hears only music and the team hears nothing from the caller:
- the phone (SIP) participant is force-unsubscribed from every person's audio, and every
  person from the phone's audio (server-side, not left to the browser);
- the agents/hold_music.py worker is dispatched into the room and publishes a track named
  "hold-music" (the softphone never plays that track, so only the caller hears it).
Resume removes the music worker and re-subscribes both directions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import structlog

from app.errors import ConflictError, FeatureUnavailableError
from app.models.voice import TERMINAL_CALL_STATUSES

log = structlog.get_logger("call_hold")

#: Participant attribute the hold-music worker sets on itself.
ROLE_ATTRIBUTE = "csaas.role"
HOLD_ROLE = "hold-music"


def enabled(settings) -> bool:  # noqa: ANN001
    return bool(getattr(settings, "hold_music_agent_name", ""))


def _is_phone(p: dict) -> bool:
    return bool((p.get("attributes") or {}).get("sip.callID")) or p.get("kind") in ("SIP", 3)


def _is_hold_music(p: dict) -> bool:
    return (p.get("attributes") or {}).get(ROLE_ATTRIBUTE) == HOLD_ROLE


def _is_worker(p: dict) -> bool:
    return p.get("kind") in ("AGENT", 4, "EGRESS", 1, "INGRESS", 2)


def _audio_sids(p: dict) -> list[str]:
    return [
        t["sid"]
        for t in (p.get("tracks") or [])
        if t.get("sid") and str(t.get("type", "AUDIO")) in ("AUDIO", "0")
    ]


async def _subscribe_all(api, room: str, participants: list[dict], *, subscribe: bool) -> None:  # noqa: ANN001
    phones = [p for p in participants if _is_phone(p)]
    people = [
        p for p in participants
        if not _is_phone(p) and not _is_hold_music(p) and not _is_worker(p)
    ]
    people_sids = [sid for p in people for sid in _audio_sids(p)]
    phone_sids = [sid for p in phones for sid in _audio_sids(p)]
    for phone in phones:
        if people_sids:
            await api.update_subscriptions(
                room=room, identity=phone["identity"], track_sids=people_sids, subscribe=subscribe
            )
    for person in people:
        if phone_sids:
            await api.update_subscriptions(
                room=room, identity=person["identity"], track_sids=phone_sids, subscribe=subscribe
            )


async def set_hold(session, api, settings, call, *, on: bool) -> None:  # noqa: ANN001
    """Put a room call on hold (on=True) or resume it. Idempotent."""
    extra = dict(call.extra or {})
    room = extra.get("room")
    if extra.get("via") != "livekit" or not room:
        raise ConflictError("Hold is available on calls made or answered in the app")
    if call.status in TERMINAL_CALL_STATUSES:
        raise ConflictError("This call has ended")
    if api is None or not enabled(settings):
        raise FeatureUnavailableError("Hold is not available")
    if bool(extra.get("on_hold")) == on:
        return

    participants = await api.list_participants(room)
    if on:
        await _subscribe_all(api, room, participants, subscribe=False)
        await api.create_agent_dispatch(
            room=room,
            agent_name=settings.hold_music_agent_name,
            metadata=json.dumps({"call_id": str(call.id)}),
        )
        extra["on_hold"] = {"since": datetime.now(timezone.utc).isoformat()}
    else:
        for p in participants:
            if _is_hold_music(p):
                try:
                    await api.remove_participant(room, p["identity"])
                except Exception:  # noqa: BLE001 - it may have left already
                    log.warning("hold_music_remove_failed", call_id=str(call.id))
        await _subscribe_all(api, room, participants, subscribe=True)
        extra.pop("on_hold", None)
    call.extra = extra
    await session.commit()
