"""Tenant-scoped knowledge base ingestion for uploads, URLs, and pasted text.

The goal of this module is to turn customer input into a ``KbDocument`` plus its
``KbChunk`` rows without ever raising for bad input: unreadable files, unsupported
types, unreachable URLs and failed fetches are all STORED as ``status="failed"`` so
the customer sees the failure in the list instead of getting a 500.
"""

from __future__ import annotations

import html as html_module
import io
import ipaddress
import os
import re
import uuid
from urllib.parse import urlsplit

import httpx
import sqlalchemy as sa
import structlog
from docx import Document as DocxDocument
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import KbChunk, KbDocument
from app.services import kb as kb_svc

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_URL_BYTES = 10 * 1024 * 1024  # 10 MB
URL_TIMEOUT_SECONDS = 15.0
SUPPORTED_EXTENSIONS = ("txt", "md", "markdown", "text", "pdf", "docx")

_script_style_re = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE
)
_block_breaks_re = re.compile(r"<(?:br\s*/?|/p|/div|/li|/h[1-6])\s*>", re.IGNORECASE)
_any_tag_re = re.compile(r"<[^>]+>")


def extension_of(filename: str) -> str:
    """Lowercase extension with no dot, ``""`` when none."""
    if not filename:
        return ""
    return os.path.splitext(filename)[1].lstrip(".").lower()


def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    """Return ``(text, error)``; exactly one of the two is non-empty."""
    ext = extension_of(filename)
    if ext in ("txt", "md", "markdown", "text", ""):
        text = data.decode("utf-8", errors="replace")
        if not text.strip():
            return "", "That file did not contain any readable text."
        return text, ""
    if ext == "pdf":
        return _extract_pdf_text(data)
    if ext == "docx":
        return _extract_docx_text(data)
    return "", "We can read text, Markdown, PDF and Word files."


def _extract_pdf_text(data: bytes) -> tuple[str, str]:
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except (PdfReadError, ValueError, KeyError):
        return "", "We could not read that PDF file."
    text = "\n".join(pages)
    if not text.strip():
        return "", "That file did not contain any readable text."
    return text, ""


def _extract_docx_text(data: bytes) -> tuple[str, str]:
    try:
        document = DocxDocument(io.BytesIO(data))
        paragraphs = [p.text for p in document.paragraphs]
    except Exception:
        # python-docx raises a mix of its own errors and lxml's underlying
        # parse errors for a corrupt or non-docx file; any of them means the
        # same thing to the customer: this is not readable.
        return "", "We could not read that Word file."
    text = "\n".join(paragraphs)
    if not text.strip():
        return "", "That file did not contain any readable text."
    return text, ""


def html_to_text(html: str) -> str:
    """Plain HTML-to-text strip with no new dependency."""
    text = _script_style_re.sub("", html)
    text = _block_breaks_re.sub("\n", text)
    text = _any_tag_re.sub("", text)
    text = html_module.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def is_public_http_url(url: str) -> bool:
    """SSRF guard for customer-supplied web addresses.

    This server fetches URLs on the customer's behalf. It must never be able to
    reach loopback, link-local, private, or metadata addresses.
    """
    try:
        parts = urlsplit(url)
    except (ValueError, AttributeError):
        return False

    if parts.scheme.lower() not in ("http", "https"):
        return False
    if not parts.hostname:
        return False
    if "@" in parts.netloc:
        return False

    hostname = parts.hostname.lower()
    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
        or hostname == "metadata.google.internal"
    ):
        return False

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return True

    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return False

    return True


async def _store(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    title: str,
    source: str,
    storage_key: str | None,
    text: str,
    error: str,
) -> KbDocument:
    """Create or replace a KB document. Caller commits; this only flushes."""
    logger = structlog.get_logger("kb.ingest")

    clean_title = (title or "").strip()[:255]
    if not clean_title:
        clean_title = "Untitled"

    existing = (
        await session.execute(
            sa.select(KbDocument).where(
                KbDocument.org_id == org_id, KbDocument.title == clean_title
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        doc = existing
        await session.execute(
            sa.delete(KbChunk).where(
                KbChunk.org_id == org_id, KbChunk.document_id == doc.id
            )
        )
    else:
        doc = KbDocument(
            id=uuid.uuid4(), org_id=org_id, title=clean_title, source=source
        )
        session.add(doc)

    doc.source = source
    doc.storage_key = storage_key
    # The document row must be INSERTed before any chunk that points at it. There is no
    # ORM relationship() between KbDocument and KbChunk - only a bare FK column - so
    # SQLAlchemy's unit of work has nothing to topologically sort on and would happily
    # emit the chunk INSERTs first, which SQLite rejects outright with FOREIGN KEY
    # constraint failed. kb.create_document flushes here for exactly this reason.
    await session.flush()

    if error:
        doc.status = "failed"
        doc.chunk_count = 0
        doc._ingest_detail = error
        await session.flush()
        logger.info(
            "kb_document_stored",
            org_id=str(org_id),
            title=clean_title,
            status=doc.status,
        )
        return doc

    pieces = kb_svc.chunk_text(text)[: kb_svc.MAX_CHUNKS_PER_DOCUMENT]
    if not pieces:
        doc.status = "failed"
        doc.chunk_count = 0
        doc._ingest_detail = "That file did not contain any readable text."
        await session.flush()
        logger.info(
            "kb_document_stored",
            org_id=str(org_id),
            title=clean_title,
            status=doc.status,
        )
        return doc

    for seq, piece in enumerate(pieces):
        session.add(
            KbChunk(
                id=uuid.uuid4(),
                org_id=org_id,
                document_id=doc.id,
                seq=seq,
                text=piece,
            )
        )

    doc.status = "indexed"
    doc.chunk_count = len(pieces)
    doc._ingest_detail = ""
    await session.flush()

    logger.info(
        "kb_document_stored",
        org_id=str(org_id),
        title=clean_title,
        status=doc.status,
    )
    return doc


async def ingest_upload(
    session: AsyncSession, org_id: uuid.UUID, *, title: str, filename: str, data: bytes
) -> KbDocument:
    """Ingest a multipart upload without parsing PDF/DOCX bytes."""
    if len(data) > MAX_UPLOAD_BYTES:
        return await _store(
            session,
            org_id,
            title=title,
            source="upload",
            storage_key=filename[:255],
            text="",
            error="That file is too big.",
        )

    text, error = extract_text(filename, data)
    return await _store(
        session,
        org_id,
        title=title,
        source="upload",
        storage_key=filename[:255],
        text=text,
        error=error,
    )


async def ingest_url(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    title: str,
    url: str,
    client: httpx.AsyncClient | None = None,
) -> KbDocument:
    """Fetch a customer URL after SSRF guards, then ingest extracted text."""
    if not is_public_http_url(url):
        return await _store(
            session,
            org_id,
            title=title,
            source="url",
            storage_key=url[:255],
            text="",
            error="That web address cannot be reached from here.",
        )

    # is_public_http_url only inspects the address syntactically (and rejects literal
    # private IPs). A DNS name can still resolve to a private A record, so reuse the
    # outbound-webhook guard which resolves the host and rejects private/loopback/link-
    # local/reserved answers. It raises ValidationFailedError; this module must never
    # raise, so convert it to a stored failure.
    # Lazy import avoids a module-level cycle between services.
    from app.services import webhooks_out as webhooks_out_svc

    hostname = urlsplit(url).hostname
    if hostname:
        try:
            await webhooks_out_svc._reject_private_target(hostname)
        except webhooks_out_svc.ValidationFailedError:
            return await _store(
                session,
                org_id,
                title=title,
                source="url",
                storage_key=url[:255],
                text="",
                error="That web address cannot be reached from here.",
            )

    should_close = client is None
    _client = client or httpx.AsyncClient(
        follow_redirects=True, timeout=URL_TIMEOUT_SECONDS
    )

    try:
        # Stream instead of materialising response.content. A hostile server can stream
        # an unbounded body until the timeout if we wait for `response.content` first;
        # reading incrementally lets us stop as soon as MAX_URL_BYTES is exceeded.
        try:
            async with _client.stream(
                "GET", url, follow_redirects=True, timeout=URL_TIMEOUT_SECONDS
            ) as response:
                if not is_public_http_url(str(response.url)):
                    return await _store(
                        session,
                        org_id,
                        title=title,
                        source="url",
                        storage_key=url[:255],
                        text="",
                        error="That web address cannot be reached from here.",
                    )

                if not (200 <= response.status_code < 300):
                    return await _store(
                        session,
                        org_id,
                        title=title,
                        source="url",
                        storage_key=url[:255],
                        text="",
                        error="That web address did not return a page.",
                    )

                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > MAX_URL_BYTES:
                        return await _store(
                            session,
                            org_id,
                            title=title,
                            source="url",
                            storage_key=url[:255],
                            text="",
                            error="That page is too big.",
                        )

                text_bytes = bytes(buffer)
                try:
                    decoded = text_bytes.decode(
                        response.encoding or "utf-8", errors="replace"
                    )
                except LookupError:
                    decoded = text_bytes.decode("utf-8", errors="replace")

                content_type = response.headers.get("content-type", "").lower()
                if "html" in content_type:
                    text = html_to_text(decoded)
                elif "text/" in content_type:
                    text = decoded
                else:
                    return await _store(
                        session,
                        org_id,
                        title=title,
                        source="url",
                        storage_key=url[:255],
                        text="",
                        error="We can read text, Markdown, PDF and Word files.",
                    )

                return await _store(
                    session,
                    org_id,
                    title=title,
                    source="url",
                    storage_key=url[:255],
                    text=text,
                    error="",
                )
        except httpx.RequestError:
            return await _store(
                session,
                org_id,
                title=title,
                source="url",
                storage_key=url[:255],
                text="",
                error="We could not reach that web address.",
            )
    finally:
        if should_close:
            await _client.aclose()


async def ingest_text(
    session: AsyncSession, org_id: uuid.UUID, *, title: str, text: str
) -> KbDocument:
    """Ingest pasted plain text."""
    if not text or not text.strip():
        return await _store(
            session,
            org_id,
            title=title,
            source="text",
            storage_key=None,
            text="",
            error="There was no text to save.",
        )

    return await _store(
        session,
        org_id,
        title=title,
        source="text",
        storage_key=None,
        text=text,
        error="",
    )
