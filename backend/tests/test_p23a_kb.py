"""P23a KB ingestion tests: success, clean failure, URL SSRF guard, and tenant
scoping. These tests use the real async SQLAlchemy session from conftest and
real Org rows because KbDocument.org_id is a real foreign key.
"""

from __future__ import annotations

import io
import uuid

import httpx
import pytest
import sqlalchemy as sa
from docx import Document as DocxDocument
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

from app.db.base import set_org_context
from app.models import KbChunk, KbDocument, Org
from app.services import kb as kb_svc
from app.services import kb_ingest


def _make_pdf_bytes(text: str) -> bytes:
    """Build a small, genuinely valid one-page PDF with a real text-drawing
    content stream, so PdfReader.extract_text() reads back real text. pypdf
    can only WRITE a PDF's low-level objects (no text-layout library like
    reportlab is a project dependency), so the content stream is built by
    hand - this is the minimal PDF operator sequence to draw a string.
    """
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)

    stream_obj = StreamObject()
    stream_obj.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream_obj)

    font_dict = DictionaryObject()
    font_dict[NameObject("/Type")] = NameObject("/Font")
    font_dict[NameObject("/Subtype")] = NameObject("/Type1")
    font_dict[NameObject("/BaseFont")] = NameObject("/Helvetica")
    font_res = DictionaryObject()
    font_res[NameObject("/F1")] = writer._add_object(font_dict)
    resources = DictionaryObject()
    resources[NameObject("/Font")] = font_res
    page[NameObject("/Resources")] = resources

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_docx_bytes(text: str) -> bytes:
    document = DocxDocument()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


async def _add_org(session, name: str = "Test Org") -> Org:
    org = Org(
        id=uuid.uuid4(),
        name=name,
        slug=f"org-{uuid.uuid4()}",
        is_active=True,
    )
    session.add(org)
    await session.flush()
    set_org_context(session, org.id)
    return org


async def test_kb_ingest_text_chunks_and_marks_indexed(session):
    org = await _add_org(session, "KB Ingest Text")
    filler = "Knowledge base ingestion operates reliably and safely. "
    middle = "The zephyr quasar jumps over the velvet xylophone. "
    text = filler * 60 + middle + filler * 60

    doc = await kb_ingest.ingest_text(session, org.id, title="Long knowledge doc", text=text)

    assert doc.status == "indexed"
    assert doc.chunk_count > 1
    chunks = (
        await session.execute(
            sa.select(KbChunk)
            .where(KbChunk.org_id == org.id, KbChunk.document_id == doc.id)
            .order_by(KbChunk.seq)
        )
    ).scalars().all()
    assert [c.seq for c in chunks] == list(range(len(chunks)))
    assert len(chunks) == doc.chunk_count

    results = await kb_svc.search(session, org.id, "velvet xylophone")
    assert any("xylophone" in result["text"] for result in results)


async def test_kb_ingest_upload_txt_and_md(session):
    org = await _add_org(session, "KB Upload Txt")

    doc_txt = await kb_ingest.ingest_upload(
        session,
        org.id,
        title="TXT upload",
        filename="notes.txt",
        data=b"Hello from the txt file.",
    )
    assert doc_txt.status == "indexed"
    assert doc_txt.storage_key == "notes.txt"
    assert doc_txt.source == "upload"

    doc_md = await kb_ingest.ingest_upload(
        session,
        org.id,
        title="MD upload",
        filename="notes.md",
        data=b"# Hi\n\nHello from the markdown file.",
    )
    assert doc_md.status == "indexed"
    assert doc_md.storage_key == "notes.md"
    assert doc_md.source == "upload"


async def test_kb_ingest_pdf_and_docx_are_indexed(session):
    org = await _add_org(session, "KB PDF DOCX")

    doc_pdf = await kb_ingest.ingest_upload(
        session,
        org.id,
        title="PDF handbook",
        filename="handbook.pdf",
        data=_make_pdf_bytes("The vault code is nine four two one."),
    )
    doc_docx = await kb_ingest.ingest_upload(
        session,
        org.id,
        title="DOCX handbook",
        filename="handbook.docx",
        data=_make_docx_bytes("Our office hours are nine to five weekdays."),
    )

    assert doc_pdf.status == "indexed"
    assert doc_pdf.chunk_count >= 1
    assert doc_docx.status == "indexed"
    assert doc_docx.chunk_count >= 1

    chunk_count = (
        await session.execute(
            sa.select(sa.func.count(KbChunk.id)).where(
                KbChunk.org_id == org.id,
                KbChunk.document_id.in_([doc_pdf.id, doc_docx.id]),
            )
        )
    ).scalar_one()
    assert chunk_count >= 2

    results = await kb_svc.search(session, org.id, "vault code")
    assert any("vault code" in result["text"] for result in results)


async def test_kb_ingest_corrupt_pdf_and_docx_are_marked_failed_not_500(session):
    org = await _add_org(session, "KB Corrupt PDF DOCX")

    doc_pdf = await kb_ingest.ingest_upload(
        session,
        org.id,
        title="Corrupt PDF",
        filename="handbook.pdf",
        data=b"%PDF-1.4 not actually a pdf",
    )
    doc_docx = await kb_ingest.ingest_upload(
        session,
        org.id,
        title="Corrupt DOCX",
        filename="handbook.docx",
        data=b"PK not actually a docx",
    )

    assert doc_pdf.status == "failed"
    assert doc_pdf.chunk_count == 0
    assert doc_pdf._ingest_detail == "We could not read that PDF file."

    assert doc_docx.status == "failed"
    assert doc_docx.chunk_count == 0
    assert doc_docx._ingest_detail == "We could not read that Word file."

    chunk_count = (
        await session.execute(
            sa.select(sa.func.count(KbChunk.id)).where(
                KbChunk.org_id == org.id,
                KbChunk.document_id.in_([doc_pdf.id, doc_docx.id]),
            )
        )
    ).scalar_one()
    assert chunk_count == 0


async def test_kb_ingest_rejects_an_oversized_upload(session):
    org = await _add_org(session, "KB Oversize")

    doc = await kb_ingest.ingest_upload(
        session,
        org.id,
        title="Too big",
        filename="big.txt",
        data=b"x" * (kb_ingest.MAX_UPLOAD_BYTES + 1),
    )

    assert doc.status == "failed"
    assert doc._ingest_detail == "That file is too big."


async def test_kb_ingest_url_strips_html(session):
    org = await _add_org(session, "KB URL HTML")

    def handler(request):
        return httpx.Response(
            200,
            text=(
                "<html><head><style>b{}</style></head><body><h1>Hours</h1>"
                "<p>Nine to five.</p><script>alert(1)</script></body></html>"
            ),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        doc = await kb_ingest.ingest_url(
            session,
            org.id,
            title="Web hours",
            url="https://example.com/hours",
            client=client,
        )

    assert doc.status == "indexed"
    chunks = (
        await session.execute(
            sa.select(KbChunk)
            .where(KbChunk.org_id == org.id, KbChunk.document_id == doc.id)
            .order_by(KbChunk.seq)
        )
    ).scalars().all()
    stored = "\n".join(c.text for c in chunks)

    assert "Hours" in stored
    assert "Nine to five." in stored
    assert "alert(1)" not in stored
    assert "b{}" not in stored
    assert "<" not in stored


async def test_kb_ingest_url_refuses_private_addresses(session):
    org = await _add_org(session, "KB URL Private")
    urls = [
        "http://127.0.0.1/x",
        "http://localhost/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/x",
        "http://192.168.1.1/x",
        "http://[::1]/x",
        "https://user:pw@example.com/x",
        "file:///etc/passwd",
        "ftp://example.com/x",
    ]

    for index, url in enumerate(urls):
        assert kb_ingest.is_public_http_url(url) is False

        def _no_request(request):
            pytest.fail("no request should have been made")
            return httpx.Response(200)

        transport = httpx.MockTransport(_no_request)
        async with httpx.AsyncClient(transport=transport) as client:
            doc = await kb_ingest.ingest_url(
                session,
                org.id,
                title=f"private {index}",
                url=url,
                client=client,
            )

        assert doc.status == "failed"
        assert doc._ingest_detail == "That web address cannot be reached from here."


async def test_kb_ingest_url_refuses_a_redirect_into_a_private_address(session):
    org = await _add_org(session, "KB URL Redirect")

    def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        return httpx.Response(200, text="secret page", headers={"content-type": "text/plain"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        doc = await kb_ingest.ingest_url(
            session,
            org.id,
            title="Redirected",
            url="https://example.com/start",
            client=client,
        )

    assert doc.status == "failed"
    assert doc._ingest_detail == "That web address cannot be reached from here."


async def test_kb_ingest_url_rejects_a_non_text_content_type(session):
    org = await _add_org(session, "KB URL Nontext")

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=b"abc",
            headers={"content-type": "application/octet-stream"},
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        doc = await kb_ingest.ingest_url(
            session,
            org.id,
            title="Binary",
            url="https://example.com/file",
            client=client,
        )

    assert doc.status == "failed"
    assert doc._ingest_detail == "We can read text, Markdown, PDF and Word files."


async def test_kb_ingest_reingesting_the_same_title_replaces_it(session):
    org = await _add_org(session, "KB Reingest")

    first = await kb_ingest.ingest_text(
        session,
        org.id,
        title="Hours",
        text="First body opening hours are nine to five.",
    )
    second = await kb_ingest.ingest_text(
        session,
        org.id,
        title="Hours",
        text="Second body weekend hours are eleven to four.",
    )

    doc_count = (
        await session.execute(
            sa.select(sa.func.count(KbDocument.id)).where(
                KbDocument.org_id == org.id, KbDocument.title == "Hours"
            )
        )
    ).scalar_one()
    assert doc_count == 1

    doc = (
        await session.execute(
            sa.select(KbDocument).where(
                KbDocument.org_id == org.id, KbDocument.title == "Hours"
            )
        )
    ).scalar_one()
    assert doc.id == first.id == second.id

    chunks = (
        await session.execute(
            sa.select(KbChunk)
            .where(KbChunk.org_id == org.id, KbChunk.document_id == doc.id)
            .order_by(KbChunk.seq)
        )
    ).scalars().all()
    whole = " ".join(c.text for c in chunks)

    assert "Second body" in whole
    assert "First body" not in whole
    assert len(chunks) == doc.chunk_count


async def test_kb_ingest_is_tenant_scoped(session):
    org_a = await _add_org(session, "KB Tenant A")
    await kb_ingest.ingest_text(
        session,
        org_a.id,
        title="Shared title",
        text="alpha secret phrase for tenant A",
    )

    org_b = await _add_org(session, "KB Tenant B")
    await kb_ingest.ingest_text(
        session,
        org_b.id,
        title="Shared title",
        text="beta secret phrase for tenant B",
    )

    set_org_context(session, org_a.id)
    docs_a = (
        await session.execute(
            sa.select(KbDocument).where(
                KbDocument.org_id == org_a.id, KbDocument.title == "Shared title"
            )
        )
    ).scalars().all()
    assert len(docs_a) == 1
    assert docs_a[0].org_id == org_a.id

    set_org_context(session, org_b.id)
    docs_b = (
        await session.execute(
            sa.select(KbDocument).where(
                KbDocument.org_id == org_b.id, KbDocument.title == "Shared title"
            )
        )
    ).scalars().all()
    assert len(docs_b) == 1
    assert docs_b[0].org_id == org_b.id

    set_org_context(session, org_a.id)
    results_a = await kb_svc.search(session, org_a.id, "alpha")
    assert any("alpha" in result["text"] for result in results_a)
    assert all("beta" not in result["text"] for result in results_a)

    set_org_context(session, org_b.id)
    results_b = await kb_svc.search(session, org_b.id, "beta")
    assert any("beta" in result["text"] for result in results_b)
    assert all("alpha" not in result["text"] for result in results_b)


def test_is_public_http_url_accepts_normal_addresses():
    assert kb_ingest.is_public_http_url("https://example.com/a") is True
    assert kb_ingest.is_public_http_url("http://example.com") is True
    assert kb_ingest.is_public_http_url("https://sub.domain.example.co.uk/x?y=1") is True


# ==================================================================================
# The HTTP surface. The service tests above prove the extraction rules; these prove the
# route's three input shapes, the "failed is a 201, never a 500" contract, and that the
# documents are org-scoped and permission-gated like every other settings surface.
# ==================================================================================
from tests.conftest import auth_headers, create_org, register_and_login  # noqa: E402


async def _kb_org(client, email: str, name: str):
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    return token, org, auth_headers(token, org["id"])


async def test_kb_document_routes_accept_text_and_multipart(client):
    _, _, headers = await _kb_org(client, "p23a-kbroute-text@example.com", "KB Route")

    pasted = await client.post(
        "/api/v1/agent/kb/documents",
        json={"title": "Hours", "text": "We are open from nine to five on weekdays."},
        headers=headers,
    )
    assert pasted.status_code == 201, pasted.text
    assert pasted.json()["status"] == "indexed"
    assert pasted.json()["source"] == "text"
    assert pasted.json()["chunk_count"] >= 1

    uploaded = await client.post(
        "/api/v1/agent/kb/documents",
        files={"file": ("policy.txt", b"Refunds within thirty days.", "text/plain")},
        data={"title": "Refund policy"},
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["status"] == "indexed"
    assert uploaded.json()["source"] == "upload"

    listed = await client.get("/api/v1/agent/kb/documents", headers=headers)
    assert listed.status_code == 200, listed.text
    assert {row["title"] for row in listed.json()} == {"Hours", "Refund policy"}

    deleted = await client.delete(
        f"/api/v1/agent/kb/documents/{pasted.json()['id']}", headers=headers
    )
    assert deleted.status_code == 204, deleted.text
    remaining = await client.get("/api/v1/agent/kb/documents", headers=headers)
    assert [row["title"] for row in remaining.json()] == ["Refund policy"]


async def test_kb_document_route_reports_a_failed_parse_as_201_not_500(client):
    _, _, headers = await _kb_org(client, "p23a-kbroute-fail@example.com", "KB Fail")

    r = await client.post(
        "/api/v1/agent/kb/documents",
        files={"file": ("handbook.pdf", b"%PDF-1.7 not really", "application/pdf")},
        headers=headers,
    )
    # A document we cannot read is a STORED failure the customer can see, not a 500.
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "failed"
    assert r.json()["detail"] == "We could not read that PDF file."
    assert r.json()["chunk_count"] == 0

    good = await client.post(
        "/api/v1/agent/kb/documents",
        files={
            "file": (
                "handbook.pdf",
                _make_pdf_bytes("Return policy: thirty days, receipt required."),
                "application/pdf",
            )
        },
        headers=headers,
    )
    assert good.status_code == 201, good.text
    assert good.json()["status"] == "indexed"
    assert good.json()["chunk_count"] >= 1

    empty = await client.post(
        "/api/v1/agent/kb/documents", json={"title": "Nothing"}, headers=headers
    )
    assert empty.status_code == 422, empty.text

    not_a_file = await client.post(
        "/api/v1/agent/kb/documents",
        data={"file": "just-a-string", "title": "Nope"},
        headers=headers,
    )
    assert not_a_file.status_code == 422, not_a_file.text


async def test_kb_documents_are_tenant_scoped_and_permission_gated(client, session):
    token_a, org_a, headers_a = await _kb_org(
        client, "p23a-kbroute-a@example.com", "KB Route A"
    )
    _, _, headers_b = await _kb_org(client, "p23a-kbroute-b@example.com", "KB Route B")

    created = await client.post(
        "/api/v1/agent/kb/documents",
        json={"title": "A only", "text": "Alpha content for org A."},
        headers=headers_a,
    )
    assert created.status_code == 201, created.text
    doc_id = created.json()["id"]

    assert (await client.get("/api/v1/agent/kb/documents", headers=headers_b)).json() == []
    cross = await client.delete(
        f"/api/v1/agent/kb/documents/{doc_id}", headers=headers_b
    )
    assert cross.status_code == 404, cross.text

    import uuid as _uuid

    from app.models import OrgMembership, Role
    from app.repositories import users as users_repo

    email = "p23a-kbroute-member@example.com"
    member_token = await register_and_login(client, email)
    user = await users_repo.get_by_email(session, email)
    org_id = _uuid.UUID(org_a["id"])
    set_org_context(session, org_id)
    role = Role(id=_uuid.uuid4(), org_id=org_id, name=email, permissions=["contacts:read"])
    session.add(role)
    await session.flush()
    session.add(
        OrgMembership(id=_uuid.uuid4(), org_id=org_id, user_id=user.id, role_id=role.id)
    )
    await session.commit()

    member_headers = auth_headers(member_token, org_a["id"])
    assert (
        await client.get("/api/v1/agent/kb/documents", headers=member_headers)
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/agent/kb/documents",
            json={"title": "Sneaky", "text": "nope"},
            headers=member_headers,
        )
    ).status_code == 403
