"""Per-user bell notifications.

A notification is a row in the user's personal bell. ``dedupe_key`` is an
idempotency key only - application logic never changes behaviour based on it;
it simply stops a repeating background job (or a retried send path) from
spamming the same bell entry. The websocket push is fire-and-forget.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import (
    NOTIFICATION_KINDS,
    Call,
    Department,
    DepartmentMember,
    Inbox,
    InboxGrant,
    MessageThread,
    Notification,
    Org,
    OrgMembership,
    OrgNumber,
    Role,
)

log = structlog.get_logger("notifications")

#: how far back missed_call_tick looks for calls it has not yet announced
MISSED_CALL_LOOKBACK_MINUTES = 120
#: one tick never announces more than this many calls per org
MISSED_CALL_BATCH = 200
#: mirrors conversations.py's own list - a terminal inbound status that reads as
#: "missed" to a human
MISSED_CALL_STATUSES = frozenset({"no_answer", "busy", "canceled", "failed"})


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def bus_from_session(session: AsyncSession) -> object | None:
    """Return the event-bus handle webhooks.py stashes on the session, if any."""
    return session.info.get("event_bus")


async def create(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    kind: str,
    body: str,
    thread_id: uuid.UUID | None = None,
    dedupe_key: str | None = None,
    bus: object | None = None,
) -> Notification | None:
    """Insert one bell row. Caller owns the transaction - this function never commits."""
    if kind not in NOTIFICATION_KINDS:
        raise ValueError(f"Unknown notification kind: {kind}")

    body = body[:255]

    if dedupe_key is not None:
        existing = (
            await session.execute(
                sa.select(Notification.id).where(
                    Notification.user_id == user_id,
                    Notification.dedupe_key == dedupe_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None

    row = Notification(
        id=uuid.uuid4(),
        org_id=org_id,
        user_id=user_id,
        kind=kind,
        thread_id=thread_id,
        body=body,
        dedupe_key=dedupe_key,
    )
    # SAVEPOINT, not a plain flush: the select above loses the race under real
    # concurrency and the unique constraint is what actually holds the line. Rolling the
    # WHOLE transaction back on that collision would also throw away the caller's own
    # work - the note that caused the mention, or the thread that was just marked
    # overdue. A nested transaction rolls back ONLY this insert and leaves the caller's
    # transaction intact.
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        set_org_context(session, org_id)
        return None

    if bus is not None:
        # The bell needs (our_e164, contact_e164) to navigate straight to the
        # conversation - a thread_id alone addresses nothing in this app (P20c: a
        # conversation is addressed by the number pair, not its thread id). Session-cached
        # via the identity map, so the mention/overdue loops that call create() once per
        # recipient with the same thread_id only hit the DB the first time.
        our_e164: str | None = None
        contact_e164: str | None = None
        if thread_id is not None:
            thread = await session.get(MessageThread, thread_id)
            if thread is not None:
                our_e164 = thread.our_e164
                contact_e164 = thread.contact_e164

        event = {
            "type": "notification.created",
            "user_id": str(user_id),
            "notification_id": str(row.id),
            "kind": kind,
            "thread_id": str(thread_id) if thread_id is not None else None,
            "our_e164": our_e164,
            "contact_e164": contact_e164,
            "body": body,
            "created_at": _aware(row.created_at).isoformat()
            if row.created_at is not None
            else None,
        }
        try:
            bus.publish(org_id, event)
        except Exception:
            # A dead websocket consumer must never fail the write that caused this.
            log.warning("notification_event_publish_failed", exc_info=True)

    return row


async def list_for_user(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    unread_only: bool = False,
    limit: int = 50,
) -> list[Notification]:
    limit = max(1, min(limit, 200))
    stmt = sa.select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    stmt = stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def thread_pairs_by_id(
    session: AsyncSession,
    thread_ids: list[uuid.UUID] | set[uuid.UUID],
) -> dict[uuid.UUID, tuple[str, str]]:
    """Return {thread_id: (our_e164, contact_e164)} in one query.

    Lets the bell navigate straight to a conversation, which is addressed by that
    number pair, not by thread id (P20c). Empty input -> empty dict, no query.
    """
    if not thread_ids:
        return {}

    rows = (
        await session.execute(
            sa.select(MessageThread.id, MessageThread.our_e164, MessageThread.contact_e164).where(
                MessageThread.id.in_(list(thread_ids))
            )
        )
    ).all()
    return {thread_id: (our_e164, contact_e164) for thread_id, our_e164, contact_e164 in rows}


async def unread_count(session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> int:
    return (
        await session.execute(
            sa.select(sa.func.count(Notification.id)).where(
                Notification.user_id == user_id,
                Notification.read_at.is_(None),
            )
        )
    ).scalar_one()


async def mark_read(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    ids: list[uuid.UUID] | None = None,
    all_: bool = False,
) -> int:
    if not all_ and not ids:
        return 0

    stmt = sa.update(Notification).where(
        Notification.user_id == user_id,
        Notification.read_at.is_(None),
    )
    if not all_:
        stmt = stmt.where(Notification.id.in_(ids))
    stmt = stmt.values(read_at=datetime.now(timezone.utc))

    result = await session.execute(stmt)
    return result.rowcount


async def notify_mention(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    thread_id: uuid.UUID,
    note_id: uuid.UUID,
    author_name: str,
    user_ids: set[uuid.UUID] | list[uuid.UUID],
    bus: object | None = None,
) -> int:
    created = 0
    for user_id in set(user_ids):
        row = await create(
            session,
            org_id,
            user_id=user_id,
            kind="mention",
            body=f"{author_name} mentioned you in a conversation",
            thread_id=thread_id,
            dedupe_key=f"mention:{note_id}:{user_id}",
            bus=bus,
        )
        if row is not None:
            created += 1
    return created


async def notify_assignment(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    thread_id: uuid.UUID,
    user_id: uuid.UUID,
    actor_name: str,
    bus: object | None = None,
) -> Notification | None:
    return await create(
        session,
        org_id,
        user_id=user_id,
        kind="assignment",
        body=f"{actor_name} assigned a conversation to you",
        thread_id=thread_id,
        dedupe_key=f"assignment:{thread_id}:{user_id}",
        bus=bus,
    )


async def notify_overdue(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    thread_id: uuid.UUID,
    user_ids: set[uuid.UUID] | list[uuid.UUID],
    rule: str,
    bus: object | None = None,
) -> int:
    assert rule in {"first", "resolution"}
    body = (
        "A conversation is still waiting for a first reply"
        if rule == "first"
        else "A conversation has been open longer than your target"
    )
    dedupe_key = f"overdue:{thread_id}:{rule}"

    created = 0
    for user_id in set(user_ids):
        row = await create(
            session,
            org_id,
            user_id=user_id,
            kind="overdue",
            body=body,
            thread_id=thread_id,
            dedupe_key=dedupe_key,
            bus=bus,
        )
        if row is not None:
            created += 1
    return created


async def notify_missed_call(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    call: Call,
    user_ids: set[uuid.UUID] | list[uuid.UUID],
    bus: object | None = None,
) -> int:
    created = 0
    for user_id in set(user_ids):
        row = await create(
            session,
            org_id,
            user_id=user_id,
            kind="missed_call",
            body=f"Missed call from {call.contact_e164}",
            thread_id=None,
            dedupe_key=f"missed_call:{call.id}:{user_id}",
            bus=bus,
        )
        if row is not None:
            created += 1
    return created


async def recipients_for_number(session: AsyncSession, e164: str) -> set[uuid.UUID]:
    """Resolve who may see the inbox on ``e164``.

    This is the inverse direction of ``services/inbox_access.py`` - that module
    answers "what may THIS user see"; this answers "who may see THIS number".
    It intentionally does not modify inbox_access.py.
    """
    recipients: set[uuid.UUID] = set()

    inbox_id = (
        await session.execute(
            sa.select(Inbox.id)
            .join(OrgNumber, OrgNumber.id == Inbox.number_id)
            .where(OrgNumber.e164 == e164)
        )
    ).scalar_one_or_none()
    if inbox_id is None:
        return recipients

    direct_user_grants = (
        await session.execute(
            sa.select(InboxGrant.grantee_id).where(
                InboxGrant.inbox_id == inbox_id,
                InboxGrant.grantee_type == "user",
            )
        )
    ).scalars().all()
    recipients.update(direct_user_grants)

    department_grants = (
        await session.execute(
            sa.select(InboxGrant.grantee_id).where(
                InboxGrant.inbox_id == inbox_id,
                InboxGrant.grantee_type == "department",
            )
        )
    ).scalars().all()
    if department_grants:
        department_members = (
            await session.execute(
                sa.select(DepartmentMember.user_id)
                .join(Department, Department.id == DepartmentMember.department_id)
                .where(
                    DepartmentMember.department_id.in_(department_grants),
                    Department.is_active.is_(True),
                )
            )
        ).scalars().all()
        recipients.update(department_members)

    role_rows = (
        await session.execute(
            sa.select(OrgMembership.user_id, Role.permissions).join(
                Role, Role.id == OrgMembership.role_id
            )
        )
    ).all()
    for user_id, permissions in role_rows:
        if permissions and ("*" in permissions or "inboxes:admin" in permissions):
            recipients.add(user_id)

    return recipients


async def missed_call_tick(session: AsyncSession, bus: object | None = None) -> dict[str, int]:
    """Sweep all orgs for recent missed inbound calls and announce each once."""
    org_ids = list(
        (
            await session.execute(
                sa.select(Org.id).execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=MISSED_CALL_LOOKBACK_MINUTES)
    notification_count = 0

    for org_id in org_ids:
        set_org_context(session, org_id)
        try:
            calls = (
                (
                    await session.execute(
                        sa.select(Call)
                        .where(
                            Call.direction == "inbound",
                            Call.status.in_(MISSED_CALL_STATUSES),
                            sa.func.coalesce(Call.ended_at, Call.created_at) >= cutoff,
                        )
                        .order_by(
                            sa.func.coalesce(Call.ended_at, Call.created_at).desc(),
                            Call.id.desc(),
                        )
                        .limit(MISSED_CALL_BATCH)
                    )
                )
                .scalars()
                .all()
            )

            # Cached per number within this org - the recipient set is identical for
            # every call that arrived on the same line during this tick.
            recipient_cache: dict[str, set[uuid.UUID]] = {}
            for call in calls:
                recipients = recipient_cache.get(call.our_e164)
                if recipients is None:
                    recipients = await recipients_for_number(session, call.our_e164)
                    recipient_cache[call.our_e164] = recipients

                if recipients:
                    # The dedupe key on each notification makes re-running this tick
                    # over the same lookback window a no-op.
                    notification_count += await notify_missed_call(
                        session,
                        org_id,
                        call=call,
                        user_ids=recipients,
                        bus=bus,
                    )
                await session.commit()
        except Exception:
            await session.rollback()
            log.exception("missed_call_tick_org_failed", org_id=str(org_id))
            continue

    return {"orgs": len(org_ids), "notifications": notification_count}
