"""Org knowledge base: server-side chunking + honest keyword search.

v1 retrieval is deliberately NOT vector search (see KbChunk's docstring): a SQL ILIKE
prefilter narrows candidates, then a simple term-frequency + title-bonus score (computed
in Python, not SQL) ranks them. pgvector is the named upgrade path, not smuggled in here.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import NotFoundError
from app.models import KbChunk, KbDocument

#: Target chunk size in characters. Not a hard cap - a chunk grows past it only when no
#: sentence boundary is found in the lookback window (see chunk_text).
CHUNK_SIZE = 1000
#: How far back from a hard cutoff to search for a ". " to break on instead.
SENTENCE_LOOKBACK = 200
#: Query terms shorter than this are noise words ("a", "to", "is") and are dropped -
#: matching every chunk that merely contains common short words is not "search".
MIN_TERM_LEN = 3
TITLE_BONUS = 2
SEARCH_LIMIT = 4


def chunk_text(text: str) -> list[str]:
    """Split ``text`` into ~CHUNK_SIZE-char pieces, preferring to cut at the LAST
    sentence boundary (". ") within SENTENCE_LOOKBACK chars of the hard CHUNK_SIZE
    cutoff, so a chunk does not end mid-sentence when a nearby period is available.
    Falls back to a hard cut when no boundary exists in the window. Chunks are
    ``seq``-numbered by the caller in the order returned here."""
    stripped = text.strip()
    if not stripped:
        return []

    chunks: list[str] = []
    start = 0
    n = len(stripped)
    while start < n:
        end = min(start + CHUNK_SIZE, n)
        if end < n:
            window_start = max(start, end - SENTENCE_LOOKBACK)
            boundary = stripped.rfind(". ", window_start, end)
            if boundary != -1:
                end = boundary + 2  # include the period + space in this chunk
        piece = stripped[start:end].strip()
        if piece:
            chunks.append(piece)
        start = end
    return chunks


async def create_document(
    session: AsyncSession, org_id: uuid.UUID, title: str, text: str
) -> KbDocument:
    doc = KbDocument(id=uuid.uuid4(), org_id=org_id, title=title, source="pasted")
    session.add(doc)
    await session.flush()
    for seq, piece in enumerate(chunk_text(text)):
        session.add(
            KbChunk(id=uuid.uuid4(), org_id=org_id, document_id=doc.id, seq=seq, text=piece)
        )
    await session.flush()
    return doc


async def list_documents(session: AsyncSession, org_id: uuid.UUID) -> list[KbDocument]:
    rows = (
        await session.execute(
            sa.select(KbDocument).where(KbDocument.org_id == org_id).order_by(KbDocument.title)
        )
    ).scalars().all()
    return list(rows)


async def get_document_with_chunks(
    session: AsyncSession, document_id: uuid.UUID
) -> tuple[KbDocument, list[KbChunk]]:
    doc = await session.get(KbDocument, document_id)
    if doc is None:
        raise NotFoundError("Knowledge base document not found")
    chunks = (
        await session.execute(
            sa.select(KbChunk).where(KbChunk.document_id == document_id).order_by(KbChunk.seq)
        )
    ).scalars().all()
    return doc, list(chunks)


async def delete_document(session: AsyncSession, document_id: uuid.UUID) -> None:
    doc = await session.get(KbDocument, document_id)
    if doc is None:
        raise NotFoundError("Knowledge base document not found")
    await session.delete(doc)


def _terms(query: str) -> list[str]:
    return [t.lower() for t in query.split() if len(t) >= MIN_TERM_LEN]


async def search(session: AsyncSession, org_id: uuid.UUID, query: str) -> list[dict]:
    """Top SEARCH_LIMIT chunks for ``query`` within this org, newest scoring logic
    documented on the module. Requires `set_org_context` to already be bound; org_id is
    also filtered explicitly (belt-and-suspenders, same style as services/agent.py)."""
    terms = _terms(query)
    if not terms:
        return []

    # A term matching only the document's TITLE (not any chunk's text) must still pull
    # that doc's chunks into the candidate set - scoring below already awards
    # TITLE_BONUS for a title match, so a title-only hit still lands with score > 0.
    conditions = [KbChunk.text.ilike(f"%{t}%") for t in terms]
    conditions += [KbDocument.title.ilike(f"%{t}%") for t in terms]
    rows = (
        await session.execute(
            sa.select(KbChunk, KbDocument.title)
            .join(KbDocument, KbChunk.document_id == KbDocument.id)
            .where(KbDocument.org_id == org_id, sa.or_(*conditions))
        )
    ).all()

    scored: list[dict] = []
    for chunk, title in rows:
        text_lower = chunk.text.lower()
        title_lower = title.lower()
        score = 0
        for term in terms:
            score += text_lower.count(term)
            if term in title_lower:
                score += TITLE_BONUS
        if score > 0:
            scored.append({"title": title, "text": chunk.text, "score": score})

    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:SEARCH_LIMIT]
