"""Usage metering (P13 DR-2).

Every quantity is DERIVED from the source-of-truth tables, never incremented -
``rollup_day`` recomputes a whole UTC day's worth of every metric and replaces whatever
``usage_records`` row was already there (UNIQUE org_id/period_date/metric). Re-running a
day is idempotent by construction: the same inputs always produce the same outputs.

``sms_segments`` billing quantity (Opus review B3) is, PER MESSAGE, the carrier-reported
count when the carrier has reported one, else our own estimate - "carrier when present,
else estimate" is what we would actually bill, not a pure estimate total.

``reconciliation`` is THE GATE query (DR-2), and is like-for-like by construction (Opus
review B3): it compares our estimate against the carrier's own count ONLY over messages
where BOTH sides exist (`carrier IS NOT NULL`) - never our estimate for messages the
carrier hasn't reported on yet. Those still-in-flight messages are reported separately as
``pending_dlrs``, never folded into a false "mismatch". Every OTHER metric has no carrier
equivalent at all and reports ``carrier=None`` / verdict ``not_applicable`` - it would be
dishonest to invent one.

``storage_bytes`` is a SNAPSHOT (current total held, not a delta), so re-rolling an OLD
day must never overwrite its historical figure with today's live total (Opus review B4) -
``include_snapshot`` gates this: only a same-day ("today") rollup writes it; a backfill
pass for a past day leaves that day's storage_bytes row exactly as it was (or absent, if
it was never rolled up as "today" at all).

Only OUTBOUND messages a carrier could plausibly have billed count toward sms_segments/
mms_messages: ``queued`` (still held, e.g. a quiet-hours defer) and ``rejected`` (blocked
before it ever reached the carrier) are excluded (Opus review item 9) - a carrier does not
bill what it never accepted.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import (
    USAGE_METRICS,
    AgentSmsTurn,
    Call,
    CallRecording,
    MediaAsset,
    Message,
    Org,
    UsageRecord,
)

#: Relative tolerance for the reconciliation verdict (DR-2).
TOLERANCE = 0.05

#: A carrier never bills what it never accepted (Opus review item 9).
NON_BILLABLE_OUTBOUND_STATUSES = ("queued", "rejected")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


async def _billable_outbound_rows(
    session: AsyncSession, org_id: uuid.UUID, start: datetime, end: datetime
) -> list[tuple[int | None, int | None, list]]:
    """(segment_count_est, segment_count_carrier, media) for every outbound message this
    org could plausibly be billed for in [start, end)."""
    rows = (
        await session.execute(
            sa.select(Message.segment_count_est, Message.segment_count_carrier, Message.media)
            .where(
                Message.org_id == org_id,
                Message.direction == "outbound",
                Message.status.not_in(NON_BILLABLE_OUTBOUND_STATUSES),
                Message.created_at >= start,
                Message.created_at < end,
            )
        )
    ).all()
    return list(rows)


async def _compute_day(
    session: AsyncSession, org_id: uuid.UUID, day: date, *, include_snapshot: bool = True
) -> dict[str, tuple[int, int | None]]:
    """One metric -> (quantity, carrier_quantity | None). Called with the session
    already scoped to ``org_id`` by the caller. ``storage_bytes`` is OMITTED entirely
    (never zeroed) when ``include_snapshot`` is False - see module docstring, B4."""
    start, end = _day_bounds(day)
    out: dict[str, tuple[int, int | None]] = {}

    # sms_segments / mms_messages: derived from the same day's billable OUTBOUND messages
    # - segment_count_est is populated at send time (services/messaging.py); the carrier
    # side (segment_count_carrier) is populated later from the DLR, so it may be absent
    # for messages still in flight. Billing quantity is carrier-when-known else estimate
    # PER MESSAGE (DR-2/B3) - not a pure estimate total.
    outbound = await _billable_outbound_rows(session, org_id, start, end)
    sms_qty = sum(
        (carrier if carrier is not None else (est if est is not None else 1))
        for est, carrier, _media in outbound
    )
    carrier_vals = [carrier for _est, carrier, _media in outbound if carrier is not None]
    out["sms_segments"] = (sms_qty, sum(carrier_vals) if carrier_vals else None)
    out["mms_messages"] = (sum(1 for *_, media in outbound if media), None)

    # voice_minutes: ceil(duration_seconds / 60) PER CALL, summed - standard "round each
    # call up to the next full minute" billing behaviour.
    durations = (
        await session.execute(
            sa.select(Call.duration_seconds).where(
                Call.org_id == org_id,
                Call.ended_at >= start,
                Call.ended_at < end,
                Call.duration_seconds.isnot(None),
            )
        )
    ).scalars().all()
    out["voice_minutes"] = (sum(math.ceil(d / 60) for d in durations if d), None)

    # ai_sms_turns / ai_tokens: every AgentSmsTurn considered that day, regardless of
    # outcome - it is agent WORK done, not just successful replies.
    turns = (
        await session.execute(
            sa.select(AgentSmsTurn.tokens_in, AgentSmsTurn.tokens_out).where(
                AgentSmsTurn.org_id == org_id,
                AgentSmsTurn.created_at >= start,
                AgentSmsTurn.created_at < end,
            )
        )
    ).all()
    out["ai_sms_turns"] = (len(turns), None)
    out["ai_tokens"] = (sum((ti or 0) + (to or 0) for ti, to in turns), None)

    # storage_bytes: a SNAPSHOT of everything currently held (media + recordings), not a
    # delta added that day. Only written for "today" - see module docstring, B4.
    if include_snapshot:
        media_bytes = (
            await session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(MediaAsset.size_bytes), 0)).where(
                    MediaAsset.org_id == org_id, MediaAsset.status == "stored"
                )
            )
        ).scalar_one()
        recording_bytes = (
            await session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(CallRecording.size_bytes), 0)).where(
                    CallRecording.org_id == org_id, CallRecording.status == "stored"
                )
            )
        ).scalar_one()
        out["storage_bytes"] = (int(media_bytes) + int(recording_bytes), None)

    return out


async def _upsert(
    session: AsyncSession,
    org_id: uuid.UUID,
    day: date,
    metric: str,
    quantity: int,
    carrier_quantity: int | None,
) -> None:
    """Get-or-create on UNIQUE(org_id, period_date, metric). Deliberately NOT a
    dialect-specific ON CONFLICT - `db/types.py` reserves postgres-dialect imports to
    itself, and this rollup only ever runs one org/day/metric at a time from the sweeper,
    so a plain select-then-write is both portable and sufficient."""
    existing = (
        await session.execute(
            sa.select(UsageRecord).where(
                UsageRecord.org_id == org_id,
                UsageRecord.period_date == day,
                UsageRecord.metric == metric,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            UsageRecord(
                id=uuid.uuid4(),
                org_id=org_id,
                period_date=day,
                metric=metric,
                quantity=quantity,
                carrier_quantity=carrier_quantity,
            )
        )
    else:
        existing.quantity = quantity
        existing.carrier_quantity = carrier_quantity
    await session.flush()


async def rollup_day(
    session: AsyncSession, org_id: uuid.UUID | None, day: date, *, include_snapshot: bool = True
) -> dict[str, int]:
    """Recompute every USAGE_METRICS for one UTC day, for one org (or every org when
    ``org_id`` is None). Commits PER ORG - same B1/B2 discipline as
    `routing_exec.routing_tick`: one org's failure must not roll back another's already-
    written rollup in the same pass.

    ``include_snapshot=False`` (Opus review B4) skips ``storage_bytes`` for this day
    entirely rather than zeroing/overwriting it - use this for any day that is NOT
    "today" (a backfill or a re-roll of a past day), since storage_bytes is a live
    snapshot and would otherwise silently rewrite that day's history with today's total.
    """
    if org_id is not None:
        org_ids = [org_id]
    else:
        org_ids = list(
            (
                await session.execute(
                    sa.select(Org.id).execution_options(**{ALLOW_UNSCOPED_KEY: True})
                )
            )
            .scalars()
            .all()
        )

    counts = {"orgs": 0, "metrics_written": 0}
    for oid in org_ids:
        set_org_context(session, oid)
        quantities = await _compute_day(session, oid, day, include_snapshot=include_snapshot)
        for metric, (qty, carrier_qty) in quantities.items():
            await _upsert(session, oid, day, metric, qty, carrier_qty)
        await session.commit()
        counts["orgs"] += 1
        counts["metrics_written"] += len(quantities)
    return counts


async def usage_tick(session: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Sweeper-driven: rolls up yesterday + today for every org, per DR-2 ("today" keeps
    catching up intraday activity; "yesterday" is re-rolled in case a late-arriving DLR
    changed the carrier side after midnight). Only "today" carries the storage_bytes
    snapshot (B4) - yesterday's re-roll must not overwrite it with today's live total."""
    moment = now or _now()
    today = moment.date()
    yesterday = today - timedelta(days=1)
    totals = {"orgs": 0, "metrics_written": 0}

    counts_yesterday = await rollup_day(session, None, yesterday, include_snapshot=False)
    counts_today = await rollup_day(session, None, today, include_snapshot=True)
    totals["orgs"] = counts_yesterday["orgs"] + counts_today["orgs"]
    totals["metrics_written"] = (
        counts_yesterday["metrics_written"] + counts_today["metrics_written"]
    )
    return totals


async def _sms_reconciliation_facts(session: AsyncSession, org_id: uuid.UUID, day: date) -> dict:
    """Like-for-like reconciliation inputs (Opus review B3): ``ours``/``carrier`` are
    summed ONLY over messages the carrier has actually reported a segment count for -
    comparing our estimate against a carrier figure that doesn't exist yet would be a
    false mismatch, not a real one. ``pending_dlrs`` counts everything still waiting on a
    carrier report, reported as ITS OWN field rather than folded into the verdict."""
    start, end = _day_bounds(day)
    rows = await _billable_outbound_rows(session, org_id, start, end)
    reconciled = [(est, carrier) for est, carrier, _media in rows if carrier is not None]
    return {
        "ours": sum((est if est is not None else 1) for est, _carrier in reconciled),
        "carrier": sum(carrier for _est, carrier in reconciled),
        "pending_dlrs": sum(1 for _est, carrier, _media in rows if carrier is None),
        "reconciled_count": len(reconciled),
    }


def _not_applicable(metric: str, *, ours: int = 0, pending_dlrs: int = 0) -> dict:
    return {
        "metric": metric,
        "ours": ours,
        "carrier": None,
        "delta": None,
        "within_tolerance": None,
        "verdict": "not_applicable",
        "pending_dlrs": pending_dlrs,
    }


async def reconciliation(session: AsyncSession, org_id: uuid.UUID, day: date) -> list[dict]:
    """One row per USAGE_METRICS, in catalogue order. THE GATE query (DR-2)."""
    rows = {
        r.metric: r
        for r in (
            await session.execute(
                sa.select(UsageRecord).where(
                    UsageRecord.org_id == org_id, UsageRecord.period_date == day
                )
            )
        )
        .scalars()
        .all()
    }
    facts = await _sms_reconciliation_facts(session, org_id, day)

    out: list[dict] = []
    for metric in USAGE_METRICS:
        if metric != "sms_segments":
            row = rows.get(metric)
            out.append(_not_applicable(metric, ours=row.quantity if row is not None else 0))
            continue

        if facts["reconciled_count"] == 0:
            out.append(_not_applicable(metric, pending_dlrs=facts["pending_dlrs"]))
            continue

        ours, carrier = facts["ours"], facts["carrier"]
        delta = ours - carrier
        base = max(ours, carrier, 1)
        within = abs(delta) / base <= TOLERANCE
        out.append(
            {
                "metric": metric,
                "ours": ours,
                "carrier": carrier,
                "delta": delta,
                "within_tolerance": within,
                "verdict": "within_tolerance" if within else "mismatch",
                "pending_dlrs": facts["pending_dlrs"],
            }
        )
    return out
