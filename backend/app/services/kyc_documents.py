"""P41 business documents: validated, encrypted at rest, never publicly linkable.

Deliberately NOT the MMS media pipeline: that table is swept by message retention and
erasure, serves bytes from an unauthenticated signed URL, and trusts the declared content
type. Identity paperwork needs the opposite of all three.

Bytes are Fernet-encrypted with CREDENTIALS_MASTER_KEY before they reach the object store,
so a copied media volume or backup reveals nothing. Without the key, uploads answer 503.
"""

from __future__ import annotations

import hashlib
import io
import uuid

import sqlalchemy as sa
from cryptography.fernet import InvalidToken
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import FeatureUnavailableError, NotFoundError, ValidationFailedError
from app.models import KYC_DOCUMENT_KINDS, KycDocument
from app.services import credentials as credential_svc

MAX_DOCUMENTS_PER_ORG = 20

#: magic-byte prefix -> canonical content type. The declared type is ignored.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)


def sniff_content_type(data: bytes) -> str | None:
    for signature, content_type in _SIGNATURES:
        if data.startswith(signature):
            return content_type
    return None


def storage_key(org_id: uuid.UUID, document_id: uuid.UUID) -> str:
    return f"org/{org_id}/kyc/{document_id}"


def _fernet(settings: Settings):
    if not credential_svc.master_key_present(settings):
        raise FeatureUnavailableError("Document uploads need CREDENTIALS_MASTER_KEY to be set")
    return credential_svc._fernet(settings)


def _safe_filename(name: str | None) -> str:
    base = (name or "document").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in base if ch.isalnum() or ch in "._- ").strip()
    return (cleaned or "document")[:255]


async def store(
    session: AsyncSession,
    settings: Settings,
    object_store,
    *,
    org_id: uuid.UUID,
    kind: str,
    filename: str | None,
    data: bytes,
    uploaded_by: uuid.UUID | None,
) -> KycDocument:
    if kind not in KYC_DOCUMENT_KINDS:
        raise ValidationFailedError(f"Document type must be one of {', '.join(KYC_DOCUMENT_KINDS)}")
    if not data:
        raise ValidationFailedError("The file is empty")
    if len(data) > settings.kyc_document_max_bytes:
        mb = settings.kyc_document_max_bytes // 1_000_000
        raise ValidationFailedError(f"Documents must be {mb} MB or smaller")
    content_type = sniff_content_type(data)
    if content_type is None:
        raise ValidationFailedError("Upload a PDF, JPG or PNG file")
    if content_type == "application/pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            if len(reader.pages) == 0:
                raise ValueError("no pages")
        except Exception as exc:
            raise ValidationFailedError("That PDF could not be opened") from exc

    count = (
        await session.execute(
            sa.select(sa.func.count(KycDocument.id)).where(KycDocument.org_id == org_id)
        )
    ).scalar_one()
    if count >= MAX_DOCUMENTS_PER_ORG:
        raise ValidationFailedError(f"At most {MAX_DOCUMENTS_PER_ORG} documents can be uploaded")

    fernet = _fernet(settings)
    doc_id = uuid.uuid4()
    key = storage_key(org_id, doc_id)
    await object_store.put(key, fernet.encrypt(data), "application/octet-stream")
    row = KycDocument(
        id=doc_id,
        org_id=org_id,
        kind=kind,
        filename=_safe_filename(filename),
        content_type=content_type,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        storage_key=key,
        uploaded_by=uploaded_by,
    )
    session.add(row)
    return row


async def read(settings: Settings, object_store, document: KycDocument) -> bytes:
    fernet = _fernet(settings)
    ciphertext = await object_store.get(document.storage_key)
    try:
        return fernet.decrypt(ciphertext)
    except InvalidToken as exc:
        raise FeatureUnavailableError(
            "This document cannot be decrypted with the configured key"
        ) from exc


async def delete(session: AsyncSession, object_store, document: KycDocument) -> None:
    try:
        await object_store.delete(document.storage_key)
    except Exception:  # noqa: BLE001 - a missing object must not block removing the row
        pass
    await session.delete(document)


async def get(session: AsyncSession, org_id: uuid.UUID, document_id: uuid.UUID) -> KycDocument:
    row = await session.get(KycDocument, document_id)
    if row is None or row.org_id != org_id:
        raise NotFoundError("Document not found")
    return row
