"""Transcript search (P13 DR-7).

Portable-first: the LIKE-on-lower() path works identically on SQLite and Postgres and is
what the main suite exercises. On Postgres the SAME function switches to
``websearch_to_tsquery`` against the generated tsvector + GIN index the migration created
(dialect-guarded there, same rule as ``db/types.py``) - only a ``pg_only`` test can prove
that branch since SQLite has no tsvector type.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import CallTranscriptSegment
from app.models.voice import Call


def _is_postgres(session: AsyncSession) -> bool:
    return session.get_bind().dialect.name == "postgresql"


async def _like_matching_call_ids(
    session: AsyncSession, org_id: uuid.UUID, query: str, limit: int
) -> list[uuid.UUID]:
    like = f"%{query.lower()}%"
    stmt = (
        sa.select(CallTranscriptSegment.call_id, sa.func.max(Call.created_at))
        .join(Call, Call.id == CallTranscriptSegment.call_id)
        .where(Call.org_id == org_id, sa.func.lower(CallTranscriptSegment.text).like(like))
        .group_by(CallTranscriptSegment.call_id)
        .order_by(sa.func.max(Call.created_at).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [r[0] for r in rows]


async def _tsvector_matching_call_ids(
    session: AsyncSession, org_id: uuid.UUID, query: str, limit: int
) -> list[uuid.UUID]:
    """Only ever reached on Postgres (see ``search_transcripts``'s dialect switch)."""
    ts_query = sa.func.websearch_to_tsquery("english", query)
    tsv = sa.func.to_tsvector("english", CallTranscriptSegment.text)
    rank = sa.func.max(sa.func.ts_rank(tsv, ts_query))
    stmt = (
        sa.select(CallTranscriptSegment.call_id, rank)
        .join(Call, Call.id == CallTranscriptSegment.call_id)
        .where(Call.org_id == org_id, tsv.op("@@")(ts_query))
        .group_by(CallTranscriptSegment.call_id)
        .order_by(rank.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [r[0] for r in rows]


async def search_transcripts(
    session: AsyncSession, org_id: uuid.UUID, query: str, *, limit: int = 20
) -> list[dict]:
    """Results grouped by call, newest/best-ranked match first: each item carries the
    call id, contact, and every matching (or - LIKE path - every) segment with role/text/
    timestamp, so a console can render the whole exchange around the hit."""
    query = (query or "").strip()
    if not query:
        return []

    if _is_postgres(session):
        call_ids = await _tsvector_matching_call_ids(session, org_id, query, limit)
    else:
        call_ids = await _like_matching_call_ids(session, org_id, query, limit)
    if not call_ids:
        return []

    calls = {
        c.id: c
        for c in (
            await session.execute(sa.select(Call).where(Call.id.in_(call_ids)))
        )
        .scalars()
        .all()
    }
    segments = (
        (
            await session.execute(
                sa.select(CallTranscriptSegment)
                .where(CallTranscriptSegment.call_id.in_(call_ids))
                .order_by(CallTranscriptSegment.call_id, CallTranscriptSegment.at_ms)
            )
        )
        .scalars()
        .all()
    )

    needle = query.lower()
    by_call: dict[uuid.UUID, dict] = {}
    for seg in segments:
        call = calls.get(seg.call_id)
        if call is None:
            continue
        bucket = by_call.setdefault(
            seg.call_id,
            {
                "call_id": str(seg.call_id),
                "contact_e164": call.contact_e164,
                "started_at": call.created_at.isoformat(),
                "segments": [],
            },
        )
        bucket["segments"].append(
            {
                "role": seg.role,
                "text": seg.text,
                "at_ms": seg.at_ms,
                "matched": needle in seg.text.lower(),
            }
        )
    return [by_call[cid] for cid in call_ids if cid in by_call]
