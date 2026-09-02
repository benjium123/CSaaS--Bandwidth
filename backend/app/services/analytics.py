"""Analytics dashboard aggregates (P13 DR-10). Every series is ONE aggregate SQL query,
grouped by UTC day - the dashboard derives, it never counts.

Day-bucketing is pinned to UTC on Postgres (Opus review item 8): plain
``date(timestamptz)`` casts using the CONNECTION's session TimeZone setting, which is not
guaranteed to be UTC, so a day boundary could silently shift with the pool's
configuration. ``_day_bucket`` wraps the column in ``timezone('UTC', col)`` first on that
dialect. SQLite needs no such wrap - every timestamp this app writes is already a UTC
instant (`TimestampMixin` always stores `datetime.now(timezone.utc)`), and SQLite's own
`date()` has no server-side timezone setting to drift against.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentSmsTurn
from app.services import spend as spend_svc
from app.models.messaging import Message
from app.models.outbound import OutboundCampaign
from app.models.voice import TERMINAL_CALL_STATUSES, Call

#: `app.models.numbers.Campaign` is the unrelated 10DLC registration record - campaign
#: PROGRESS here means `OutboundCampaign` (P11).
_TERMINAL_MESSAGE_STATUSES = ("delivered", "failed", "rejected")


def _day_bounds(days: int, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    moment = now or datetime.now(timezone.utc)
    end = datetime(moment.year, moment.month, moment.day, tzinfo=timezone.utc) + timedelta(days=1)
    start = end - timedelta(days=days)
    return start, end


def _day_bucket(session: AsyncSession, col):  # noqa: ANN001, ANN201 - SQLAlchemy expr
    """The UTC calendar-day bucketing expression for ``col`` (see module docstring)."""
    if session.get_bind().dialect.name == "postgresql":
        return sa.func.date(sa.func.timezone("UTC", col))
    return sa.func.date(col)


async def _messages_series(
    session: AsyncSession, org_id: uuid.UUID, start: datetime, end: datetime
) -> list[dict]:
    bucket = _day_bucket(session, Message.created_at)
    stmt = (
        sa.select(
            bucket.label("day"),
            Message.direction,
            Message.status,
            sa.func.count().label("n"),
        )
        .where(Message.org_id == org_id, Message.created_at >= start, Message.created_at < end)
        .group_by(bucket, Message.direction, Message.status)
    )
    rows = (await session.execute(stmt)).all()

    by_day: dict[str, dict] = {}
    for day, direction, status, n in rows:
        key = str(day)
        bucket = by_day.setdefault(
            key, {"date": key, "inbound": 0, "outbound": 0, "_delivered": 0, "_terminal": 0}
        )
        if direction == "inbound":
            bucket["inbound"] += n
        else:
            bucket["outbound"] += n
            if status in _TERMINAL_MESSAGE_STATUSES:
                bucket["_terminal"] += n
                if status == "delivered":
                    bucket["_delivered"] += n

    out = []
    for key in sorted(by_day):
        b = by_day[key]
        rate = (b["_delivered"] / b["_terminal"]) if b["_terminal"] else None
        out.append(
            {
                "date": b["date"],
                "inbound": b["inbound"],
                "outbound": b["outbound"],
                "delivery_rate": rate,
            }
        )
    return out


async def _calls_series(
    session: AsyncSession, org_id: uuid.UUID, start: datetime, end: datetime
) -> list[dict]:
    bucket = _day_bucket(session, Call.created_at)
    stmt = (
        sa.select(
            bucket.label("day"),
            sa.func.count().label("n"),
            sa.func.avg(Call.duration_seconds).label("avg_duration"),
        )
        .where(
            Call.org_id == org_id,
            Call.created_at >= start,
            Call.created_at < end,
            Call.status.in_(TERMINAL_CALL_STATUSES),
        )
        .group_by(bucket)
        .order_by(bucket)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "date": str(day),
            "calls": n,
            "avg_duration_seconds": float(avg) if avg is not None else None,
        }
        for day, n, avg in rows
    ]


async def _campaign_progress(session: AsyncSession, org_id: uuid.UUID) -> list[dict]:
    """A SNAPSHOT (not a daily series): how many campaigns are in each status right now."""
    stmt = (
        sa.select(OutboundCampaign.status, sa.func.count())
        .where(OutboundCampaign.org_id == org_id)
        .group_by(OutboundCampaign.status)
    )
    rows = dict((await session.execute(stmt)).all())
    return [{"status": status, "count": count} for status, count in sorted(rows.items())]


async def _ai_series(
    session: AsyncSession, org_id: uuid.UUID, start: datetime, end: datetime
) -> list[dict]:
    bucket = _day_bucket(session, AgentSmsTurn.created_at)
    stmt = (
        sa.select(
            bucket.label("day"),
            AgentSmsTurn.status,
            sa.func.count().label("n"),
        )
        .where(
            AgentSmsTurn.org_id == org_id,
            AgentSmsTurn.created_at >= start,
            AgentSmsTurn.created_at < end,
        )
        .group_by(bucket, AgentSmsTurn.status)
    )
    rows = (await session.execute(stmt)).all()

    by_day: dict[str, dict] = {}
    for day, status, n in rows:
        key = str(day)
        bucket = by_day.setdefault(key, {"date": key, "turns": 0, "handoffs": 0})
        bucket["turns"] += n
        if status == "handoff":
            bucket["handoffs"] += n
    return [by_day[key] for key in sorted(by_day)]


async def overview(
    session: AsyncSession, org_id: uuid.UUID, days: int, *, now: datetime | None = None
) -> dict:
    start, end = _day_bounds(days, now=now)
    today = (now or datetime.now(timezone.utc)).date()
    month_to_date_micros = await spend_svc.month_to_date_micros(session, org_id, today)
    return {
        "range": {
            "start": start.date().isoformat(),
            "end": (end - timedelta(days=1)).date().isoformat(),
            "days": days,
        },
        "messages": await _messages_series(session, org_id, start, end),
        "calls": await _calls_series(session, org_id, start, end),
        "campaigns": await _campaign_progress(session, org_id),
        "ai": await _ai_series(session, org_id, start, end),
        "spend_usd_month_to_date": round(month_to_date_micros / 1_000_000, 2),
    }
