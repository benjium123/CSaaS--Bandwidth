"""The inbox aggregate.

Everything one row of the inbox renders, in a **fixed number of queries regardless of page
size**. Thread lists with last-message previews and unread counts are the classic N+1; the
regression test asserts the query count is independent of `limit`, so a lazy load slipping
into the serializer breaks the build rather than the latency graph.

Pagination is keyset, not offset: pages shift under live inserts, and an inbox is the most
live list in the product.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ValidationFailedError
from app.models import (
    Contact,
    Message,
    MessageThread,
    Tag,
    ThreadLabel,
    User,
)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
EPOCH_NAIVE = datetime(1970, 1, 1)
DEFAULT_LIMIT = 30
MAX_LIMIT = 100


def _is_sqlite(session: AsyncSession) -> bool:
    bind = session.get_bind()
    return bind.dialect.name == "sqlite"


def _bind_dt(session: AsyncSession, dt: datetime) -> datetime:
    """Match the value we bind to what the backend actually stores.

    SQLite persists DateTime(timezone=True) as a naive string; binding an AWARE value would
    append an offset and break the lexical comparison. Postgres needs the aware value.
    """
    return dt.replace(tzinfo=None) if _is_sqlite(session) else dt


@dataclass
class InboxFilters:
    status: str | None = None
    assigned: str | None = None  # "me" | "unassigned" | "<user_id>"
    q: str | None = None
    label_id: uuid.UUID | None = None


def _as_utc(dt: datetime) -> datetime:
    """Interpret a datetime as UTC.

    SQLite hands back NAIVE datetimes for DateTime(timezone=True) columns while Postgres
    hands back aware ones. Calling .astimezone() on a naive value silently treats it as
    LOCAL time and shifts it by the machine's UTC offset - which corrupted the pagination
    cursor by hours. Everything we write is UTC, so a naive value is stamped, not converted.
    """
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def encode_cursor(last_message_at: datetime | None, thread_id: uuid.UUID) -> str:
    ts = _as_utc(last_message_at or EPOCH).isoformat()
    return base64.urlsafe_b64encode(f"{ts}|{thread_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, _, id_str = raw.partition("|")
        return datetime.fromisoformat(ts_str), uuid.UUID(id_str)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValidationFailedError("Invalid cursor") from exc


async def list_inbox(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    filters: InboxFilters,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    limit = max(1, min(limit, MAX_LIMIT))

    # ---- QUERY 1: the page of threads -------------------------------------------
    stmt = sa.select(MessageThread)
    if filters.status:
        stmt = stmt.where(MessageThread.status == filters.status)

    if filters.assigned == "me":
        stmt = stmt.where(MessageThread.assigned_user_id == user_id)
    elif filters.assigned == "unassigned":
        stmt = stmt.where(MessageThread.assigned_user_id.is_(None))
    elif filters.assigned:
        try:
            stmt = stmt.where(MessageThread.assigned_user_id == uuid.UUID(filters.assigned))
        except ValueError as exc:
            raise ValidationFailedError("assigned must be 'me', 'unassigned' or a user id") from exc

    if filters.label_id:
        stmt = stmt.where(
            MessageThread.id.in_(
                sa.select(ThreadLabel.thread_id).where(ThreadLabel.tag_id == filters.label_id)
            )
        )

    if filters.q:
        needle = f"%{filters.q.strip().lower()}%"
        # lower()+LIKE rather than ILIKE: ILIKE is Postgres-only and the local suite runs
        # on SQLite (see the dialect-import ban, ARCHITECTURE/P0 DR-1).
        name_match = sa.select(Contact.id).where(
            sa.func.lower(Contact.display_name).like(needle)
        )
        stmt = stmt.where(
            sa.or_(
                sa.func.lower(MessageThread.contact_e164).like(needle),
                MessageThread.contact_id.in_(name_match),
            )
        )

    if cursor:
        c_ts, c_id = decode_cursor(cursor)
        c_ts = _bind_dt(session, c_ts)
        # Strict keyset: (last_message_at, id) DESC. Ties broken by id so the walk is total.
        stmt = stmt.where(
            sa.or_(
                MessageThread.last_message_at < c_ts,
                sa.and_(MessageThread.last_message_at == c_ts, MessageThread.id < c_id),
            )
        )

    stmt = stmt.order_by(
        MessageThread.last_message_at.desc().nullslast(), MessageThread.id.desc()
    ).limit(limit + 1)

    threads = list((await session.execute(stmt)).scalars().all())
    has_more = len(threads) > limit
    threads = threads[:limit]

    if not threads:
        return {"items": [], "next_cursor": None}

    ids = [t.id for t in threads]

    # ---- QUERY 2: last-message preview per thread, ONE window function ----------
    rn = (
        sa.func.row_number()
        .over(
            partition_by=Message.thread_id,
            order_by=(Message.created_at.desc(), Message.id.desc()),
        )
        .label("rn")
    )
    ranked = (
        sa.select(
            Message.id,
            Message.thread_id,
            Message.direction,
            Message.body,
            Message.status,
            Message.created_at,
            rn,
        )
        .where(Message.thread_id.in_(ids))
        .subquery()
    )
    previews = {
        row.thread_id: {
            "id": row.id,
            "direction": row.direction,
            "body": row.body,
            "status": row.status,
            "created_at": row.created_at,
        }
        for row in (
            await session.execute(sa.select(ranked).where(ranked.c.rn == 1))
        ).all()
    }

    # ---- QUERY 3: unread counts, one grouped count ------------------------------
    unread_rows = (
        await session.execute(
            sa.select(Message.thread_id, sa.func.count(Message.id))
            .join(MessageThread, MessageThread.id == Message.thread_id)
            .where(
                Message.thread_id.in_(ids),
                Message.direction == "inbound",
                Message.created_at
                > sa.func.coalesce(
                    MessageThread.last_read_at, EPOCH_NAIVE if _is_sqlite(session) else EPOCH
                ),
            )
            .group_by(Message.thread_id)
        )
    ).all()
    unread = {row[0]: row[1] for row in unread_rows}

    # ---- QUERY 4: contacts ------------------------------------------------------
    contact_ids = [t.contact_id for t in threads if t.contact_id]
    contacts = {}
    if contact_ids:
        contacts = {
            c.id: {"id": c.id, "display_name": c.display_name}
            for c in (
                await session.execute(sa.select(Contact).where(Contact.id.in_(contact_ids)))
            ).scalars().all()
        }

    # ---- QUERY 5: assignees (users are GLOBAL, not tenant-scoped) ---------------
    assignee_ids = [t.assigned_user_id for t in threads if t.assigned_user_id]
    assignees = {}
    if assignee_ids:
        assignees = {
            u.id: {"id": u.id, "full_name": u.full_name or u.email}
            for u in (
                await session.execute(sa.select(User).where(User.id.in_(assignee_ids)))
            ).scalars().all()
        }

    # ---- QUERY 6: labels --------------------------------------------------------
    label_rows = (
        await session.execute(
            sa.select(ThreadLabel.thread_id, Tag)
            .join(Tag, Tag.id == ThreadLabel.tag_id)
            .where(ThreadLabel.thread_id.in_(ids))
        )
    ).all()
    labels: dict[uuid.UUID, list] = {}
    for thread_id, tag in label_rows:
        labels.setdefault(thread_id, []).append(
            {"id": tag.id, "name": tag.name, "color": tag.color}
        )

    items = [
        {
            "thread": {
                "id": t.id,
                "our_e164": t.our_e164,
                "contact_e164": t.contact_e164,
                "status": t.status,
                "assigned_user_id": t.assigned_user_id,
                "last_message_at": t.last_message_at,
            },
            "last_message": previews.get(t.id),
            "unread": unread.get(t.id, 0),
            "contact": contacts.get(t.contact_id) if t.contact_id else None,
            "assignee": assignees.get(t.assigned_user_id) if t.assigned_user_id else None,
            "labels": labels.get(t.id, []),
        }
        for t in threads
    ]

    next_cursor = (
        encode_cursor(threads[-1].last_message_at, threads[-1].id) if has_more else None
    )
    return {"items": items, "next_cursor": next_cursor}
