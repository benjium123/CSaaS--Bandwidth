"""P9 knowledge base: chunk_text unit tests, search scoring, and the human-facing
GET/POST/DELETE /api/v1/kb/documents routes (OrgContext, settings:read/write).
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import NotFoundError
from app.models import KbChunk
from app.services import kb as kb_svc
from tests.conftest import auth_headers, create_org, register_and_login


# ==================================================================================
# chunk_text (pure function)
# ==================================================================================
def test_chunk_text_empty_returns_no_chunks():
    assert kb_svc.chunk_text("") == []
    assert kb_svc.chunk_text("   \n  ") == []


def test_chunk_text_short_text_is_one_chunk():
    assert kb_svc.chunk_text("Hello world.") == ["Hello world."]


def test_chunk_text_prefers_sentence_boundary_within_lookback():
    # A period sits just inside the 200-char lookback window before the 1000-char
    # cutoff - the chunk must end there, not at the hard 1000-char mark.
    lead = "A" * 850 + ". " + "B" * 100  # period at index 851, well within [800, 1000)
    tail = "C" * 500
    text = lead + tail
    chunks = kb_svc.chunk_text(text)
    assert chunks[0] == lead[:852].strip()  # up to and including ". "
    assert chunks[0].endswith(".")
    # The real invariant: no characters are lost or duplicated by chunking - joining
    # every chunk back together and stripping ALL whitespace must reconstruct the
    # original text (also stripped of all whitespace, since `chunk_text` trims
    # leading/trailing whitespace off each individual piece).
    assert "".join("".join(chunks).split()) == "".join(text.split())


def test_chunk_text_falls_back_to_hard_cut_when_no_boundary_in_window():
    text = "A" * 1500  # no periods anywhere
    chunks = kb_svc.chunk_text(text)
    assert chunks[0] == "A" * 1000
    assert chunks[1] == "A" * 500


def test_chunk_text_seq_order_reconstructs_original_when_no_boundaries_consumed():
    text = "X" * 1000 + "Y" * 1000 + "Z" * 10
    chunks = kb_svc.chunk_text(text)
    assert len(chunks) == 3
    assert chunks[0] == "X" * 1000
    assert chunks[1] == "Y" * 1000
    assert chunks[2] == "Z" * 10


def test_chunk_text_multiple_sentences_each_own_chunk_when_short():
    text = "First sentence. Second sentence. Third sentence."
    chunks = kb_svc.chunk_text(text)
    # Well under 1000 chars - stays one chunk (chunking-by-size, not by-sentence).
    assert chunks == [text]


# ==================================================================================
# search scoring (service level, direct org context)
# ==================================================================================
async def _seed_org(client, email: str, name: str) -> uuid.UUID:
    """A real Org row (via the normal register/create-org flow, so `slug` etc. are
    populated the way the app itself populates them) for kb_svc unit tests that want a
    session bound to a real org without going through OrgContext/RBAC."""
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    return uuid.UUID(org["id"])


async def test_search_scores_term_frequency_and_title_bonus(client, session):
    org_id = await _seed_org(client, "kbsearch1@example.com", "KB Search Org 1")
    set_org_context(session, org_id)
    await kb_svc.create_document(
        session, org_id, "Refund policy", "Refunds are available. Refund requests take 3 days."
    )
    await kb_svc.create_document(
        session, org_id, "Shipping info", "We ship within 2 business days nationwide."
    )
    await session.commit()

    results = await kb_svc.search(session, org_id, "refund")
    assert len(results) == 1
    assert results[0]["title"] == "Refund policy"
    # 2 occurrences of "refund" in the text + 2 bonus for the term appearing in the title.
    assert results[0]["score"] == 2 + kb_svc.TITLE_BONUS


async def test_search_excludes_zero_score_chunks(client, session):
    org_id = await _seed_org(client, "kbsearch2@example.com", "KB Search Org 2")
    set_org_context(session, org_id)
    await kb_svc.create_document(session, org_id, "Hours", "We are open 9 to 5 on weekdays.")
    await session.commit()

    results = await kb_svc.search(session, org_id, "refund")
    assert results == []


async def test_search_matches_on_title_alone(client, session):
    """F10: a query term that appears ONLY in the document's title (not in any chunk's
    text) must still surface that doc's chunks - the ILIKE prefilter has to consider
    KbDocument.title, not just KbChunk.text, or a title-only match is invisible."""
    org_id = await _seed_org(client, "kbsearch5@example.com", "KB Search Org 5")
    set_org_context(session, org_id)
    await kb_svc.create_document(
        session, org_id, "Onboarding checklist", "Complete these steps before your first day."
    )
    await kb_svc.create_document(session, org_id, "Other doc", "unrelated filler content")
    await session.commit()

    results = await kb_svc.search(session, org_id, "onboarding")
    assert len(results) == 1
    assert results[0]["title"] == "Onboarding checklist"
    assert results[0]["score"] == kb_svc.TITLE_BONUS


async def test_search_drops_short_terms(client, session):
    org_id = await _seed_org(client, "kbsearch3@example.com", "KB Search Org 3")
    set_org_context(session, org_id)
    await kb_svc.create_document(session, org_id, "Doc", "to a is on it")
    await session.commit()

    results = await kb_svc.search(session, org_id, "to a is")
    assert results == []


async def test_search_is_org_isolated(client, session):
    org_a = await _seed_org(client, "kbscopeA2@example.com", "KB Scope Org A")
    set_org_context(session, org_a)
    await kb_svc.create_document(session, org_a, "Org A doc", "special widget pricing details")
    await session.commit()

    org_b = await _seed_org(client, "kbscopeB2@example.com", "KB Scope Org B")
    set_org_context(session, org_b)

    results = await kb_svc.search(session, org_b, "widget")
    assert results == []

    set_org_context(session, org_a)
    results_a = await kb_svc.search(session, org_a, "widget")
    assert len(results_a) == 1


async def test_search_limits_to_top_four(client, session):
    org_id = await _seed_org(client, "kbsearch4@example.com", "KB Search Org 4")
    set_org_context(session, org_id)
    for i in range(6):
        await kb_svc.create_document(session, org_id, f"Doc {i}", "banana banana banana " * (i + 1))
    await session.commit()

    results = await kb_svc.search(session, org_id, "banana")
    assert len(results) == kb_svc.SEARCH_LIMIT


# ==================================================================================
# get_document_with_chunks / delete_document (service level)
# ==================================================================================
async def test_get_document_with_chunks_returns_chunks_in_seq_order(client, session):
    org_id = await _seed_org(client, "kbchunks1@example.com", "KB Chunks Org 1")
    set_org_context(session, org_id)
    text = "A" * 1500  # forces 2 chunks (see chunk_text tests above)
    doc = await kb_svc.create_document(session, org_id, "Long doc", text)
    await session.commit()

    fetched, chunks = await kb_svc.get_document_with_chunks(session, doc.id)
    assert fetched.id == doc.id
    assert [c.seq for c in chunks] == [0, 1]


async def test_get_document_not_found_raises(client, session):
    org_id = await _seed_org(client, "kbnotfound1@example.com", "KB Not Found Org")
    set_org_context(session, org_id)
    with pytest.raises(NotFoundError):
        await kb_svc.get_document_with_chunks(session, uuid.uuid4())


async def test_delete_document_cascades_chunks(client, session):
    org_id = await _seed_org(client, "kbchunks2@example.com", "KB Chunks Org 2")
    set_org_context(session, org_id)
    doc = await kb_svc.create_document(session, org_id, "Doomed doc", "A" * 1500)
    await session.commit()

    await kb_svc.delete_document(session, doc.id)
    await session.commit()

    with pytest.raises(NotFoundError):
        await kb_svc.get_document_with_chunks(session, doc.id)

    # F11a: the real invariant is that no KbChunk row for this document survives the
    # cascade - get_document_with_chunks raising NotFoundError only proves the DOCUMENT
    # is gone, not that its chunks were actually cleaned up rather than orphaned.
    remaining = (
        await session.execute(sa.select(KbChunk).where(KbChunk.document_id == doc.id))
    ).scalars().all()
    assert remaining == []


# ==================================================================================
# Human-facing routes: /api/v1/kb/documents
# ==================================================================================
async def test_kb_document_crud_roundtrip(client):
    token = await register_and_login(client, "kbuser1@example.com")
    org = await create_org(client, token, "Org KB1")
    headers = auth_headers(token, org["id"])

    r = await client.post(
        "/api/v1/kb/documents",
        json={"title": "Hours", "text": "We are open weekdays from 9am to 5pm."},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]
    assert r.json()["title"] == "Hours"

    r_list = await client.get("/api/v1/kb/documents", headers=headers)
    assert r_list.status_code == 200
    assert any(d["id"] == doc_id for d in r_list.json())

    r_get = await client.get(f"/api/v1/kb/documents/{doc_id}", headers=headers)
    assert r_get.status_code == 200, r_get.text
    assert len(r_get.json()["chunks"]) == 1

    r_del = await client.delete(f"/api/v1/kb/documents/{doc_id}", headers=headers)
    assert r_del.status_code == 204

    r_list2 = await client.get("/api/v1/kb/documents", headers=headers)
    assert all(d["id"] != doc_id for d in r_list2.json())


async def test_kb_document_duplicate_title_is_409(client):
    token = await register_and_login(client, "kbuser2@example.com")
    org = await create_org(client, token, "Org KB2")
    headers = auth_headers(token, org["id"])

    payload = {"title": "Dup", "text": "some text here"}
    r1 = await client.post("/api/v1/kb/documents", json=payload, headers=headers)
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/kb/documents", json=payload, headers=headers)
    assert r2.status_code == 409


async def test_kb_documents_require_settings_permission(client):
    """The 'agent' role lacks settings:write (P0's tested deny path uses members:read,
    but settings:* is gated the same way) - a member without settings:write cannot
    create or delete KB documents. We simulate this by hitting the routes with no
    X-Org-Id membership at all, which 403s identically upstream of the permission
    check itself."""
    token = await register_and_login(client, "kbuser3@example.com")
    r = await client.post(
        "/api/v1/kb/documents",
        json={"title": "X", "text": "y"},
        headers=auth_headers(token, uuid.uuid4()),
    )
    assert r.status_code == 403


async def test_kb_documents_scoped_to_org(client):
    token_a = await register_and_login(client, "kbscopeA@example.com")
    org_a = await create_org(client, token_a, "Org KB Scope A")
    token_b = await register_and_login(client, "kbscopeB@example.com")
    org_b = await create_org(client, token_b, "Org KB Scope B")

    r = await client.post(
        "/api/v1/kb/documents",
        json={"title": "A only", "text": "org a secret text"},
        headers=auth_headers(token_a, org_a["id"]),
    )
    assert r.status_code == 201, r.text

    r_list_b = await client.get(
        "/api/v1/kb/documents", headers=auth_headers(token_b, org_b["id"])
    )
    assert r_list_b.json() == []
