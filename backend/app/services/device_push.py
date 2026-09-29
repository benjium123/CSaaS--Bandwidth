"""Ringlite apps P2: fan out native push notifications to a user's live devices.

A caller hands over user ids and a payload; this module loads the users, honours their
notification preferences, sends to every FCM token whose device session is still live,
prunes tokens FCM reports as unregistered and records when a token was last used.

The fan-out runs in its OWN database session because the caller's request transaction
is already finished by the time the background task runs.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog

from app.config import Settings
from app.db.session import get_sessionmaker
from app.models import DevicePushToken, User
from app.models import Session as IdentitySession
from app.models.push import DEFAULT_NOTIFICATION_PREFS
from app.services import fcm

log = structlog.get_logger(__name__)

#: Notification kind -> the preference toggle that gates it. Kinds absent from this map
#: (logout, incoming_call, call_cancel) are delivered regardless of preferences.
PREF_FOR_KIND: dict[str, str] = {
    "new_inbound": "new_inbound",
    "missed_call": "missed_call",
    "voicemail": "missed_call",
    "mention": "mention",
    "assignment": "assignment",
    "overdue": "sla_breach",
}
#: Kinds that must never carry an Android notification block (ringing is driven from the
#: data payload by the app itself).
DATA_ONLY_KINDS: frozenset[str] = frozenset({"incoming_call", "call_cancel"})

#: Strong refs for fire-and-forget pushes (a bare create_task can be GC'd mid-flight).
_pending: set[asyncio.Task] = set()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def wants(user: User, kind: str) -> bool:
    """True when the user's preferences allow this kind (unknown kinds always pass)."""
    pref = PREF_FOR_KIND.get(kind)
    if pref is None:
        return True
    prefs = {**DEFAULT_NOTIFICATION_PREFS, **(user.notification_prefs or {})}
    return bool(prefs.get(pref, True))


async def live_tokens(session, user_ids) -> list[DevicePushToken]:
    """FCM tokens whose device session is not revoked and has not yet expired."""
    ids = list(user_ids)
    if not ids:
        return []
    rows = (
        await session.execute(
            sa.select(DevicePushToken, IdentitySession.expires_at)
            .join(IdentitySession, IdentitySession.id == DevicePushToken.session_id)
            .where(
                DevicePushToken.user_id.in_(ids),
                IdentitySession.revoked_at.is_(None),
            )
            .execution_options(allow_unscoped=True)
        )
    ).all()
    now = datetime.now(timezone.utc)
    # Compare in Python: SQLite returns naive datetimes for a tz-aware column.
    return [token for token, expires_at in rows if _aware(expires_at) > now]


async def push_to_users(
    settings: Settings,
    user_ids,
    kind: str,
    data: dict,
    *,
    title: str | None = None,
    body: str | None = None,
    high_priority: bool = False,
    collapse_key: str | None = None,
) -> None:
    if not fcm.enabled(settings):
        return
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        users = (
            await session.execute(
                sa.select(User)
                .where(User.id.in_(ids))
                .execution_options(allow_unscoped=True)
            )
        ).scalars().all()
        allowed = [user.id for user in users if wants(user, kind)]
        if not allowed:
            return
        tokens = await live_tokens(session, allowed)
        if not tokens:
            return

        payload = dict(data or {})
        payload["kind"] = kind
        notification = None
        if title and body and kind not in DATA_ONLY_KINDS:
            notification = {"title": title, "body": body, "channel_id": kind}

        results = await asyncio.gather(
            *(
                fcm.send(
                    settings,
                    token.token,
                    payload,
                    high_priority=high_priority,
                    notification=notification,
                    collapse_key=collapse_key,
                )
                for token in tokens
            )
        )
        now = datetime.now(timezone.utc)
        for token, result in zip(tokens, results):
            if result is fcm.FcmResult.OK:
                token.last_used_at = now
            elif result is fcm.FcmResult.UNREGISTERED:
                await session.delete(token)
        await session.commit()


async def push_logout(settings: Settings, session_id: uuid.UUID) -> None:
    """Tell a device it was signed out, then forget its token."""
    if not fcm.enabled(settings):
        return
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        token = (
            await session.execute(
                sa.select(DevicePushToken)
                .where(DevicePushToken.session_id == session_id)
                .execution_options(allow_unscoped=True)
            )
        ).scalar_one_or_none()
        if token is None:
            return
        await fcm.send(
            settings,
            token.token,
            {"kind": "logout"},
            high_priority=True,
        )
        await session.delete(token)
        await session.commit()


def schedule(settings: Settings, coro) -> asyncio.Task | None:
    """Run ``coro`` as a fire-and-forget task; in tests return it so the test can await it."""
    task = asyncio.create_task(_run(coro))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    if getattr(settings, "app_env", None) == "test":
        return task
    return None


async def _run(coro) -> None:
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 - a background task must not crash the loop
        log.warning("device_push.task_failed", error=str(exc))
