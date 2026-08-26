"""P9 human-facing routes: appointments the AI (or a human) booked, and the org's
knowledge base documents. Both are plain OrgContext routes - no dedicated permission
exists yet for either resource, so they reuse the closest existing ones (calls:*/
settings:* - same "no dedicated agent:* permission" call made in agent.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.auth.deps import OrgContext, require_permission
from app.errors import ConflictError
from app.models import Appointment
from app.services import agent as agent_svc
from app.services import kb as kb_svc

router = APIRouter(prefix="/api/v1", tags=["scheduling"])


# ==================================================================================
# Appointments
# ==================================================================================
class AppointmentOut(BaseModel):
    id: uuid.UUID
    call_id: uuid.UUID | None
    contact_e164: str
    raw_when: str
    scheduled_for: datetime | None
    notes: str
    status: str
    created_by: str


def _appointment_out(a: Appointment) -> AppointmentOut:
    return AppointmentOut(
        id=a.id,
        call_id=a.call_id,
        contact_e164=a.contact_e164,
        raw_when=a.raw_when,
        scheduled_for=a.scheduled_for,
        notes=a.notes,
        status=a.status,
        created_by=a.created_by,
    )


@router.get("/appointments", response_model=list[AppointmentOut])
async def list_appointments(
    ctx: Annotated[OrgContext, Depends(require_permission("calls:read"))],
    status: str | None = None,
) -> list[AppointmentOut]:
    rows = await agent_svc.list_appointments(ctx.session, ctx.org.id, status=status)
    return [_appointment_out(a) for a in rows]


class AppointmentPatch(BaseModel):
    #: See Appointment.status's docstring (app/models/scheduling.py) for the allowed
    #: values - kept as a literal here rather than a shared constant since the model
    #: itself only documents them in a comment, not as an importable vocabulary.
    status: Literal["booked", "canceled", "done"] | None = Field(default=None)
    scheduled_for: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)


@router.patch("/appointments/{appointment_id}", response_model=AppointmentOut)
async def patch_appointment(
    appointment_id: uuid.UUID,
    payload: AppointmentPatch,
    ctx: Annotated[OrgContext, Depends(require_permission("calls:place"))],
) -> AppointmentOut:
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    appt = await agent_svc.update_appointment(ctx.session, appointment_id, **updates)
    await ctx.session.commit()
    return _appointment_out(appt)


# ==================================================================================
# Knowledge base
# ==================================================================================
class KbDocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1)


class KbDocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    source: str


class KbChunkOut(BaseModel):
    seq: int
    text: str


class KbDocumentDetailOut(KbDocumentOut):
    chunks: list[KbChunkOut]


@router.get("/kb/documents", response_model=list[KbDocumentOut])
async def list_kb_documents(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> list[KbDocumentOut]:
    rows = await kb_svc.list_documents(ctx.session, ctx.org.id)
    return [KbDocumentOut(id=d.id, title=d.title, source=d.source) for d in rows]


@router.post("/kb/documents", response_model=KbDocumentOut, status_code=201)
async def create_kb_document(
    payload: KbDocumentIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> KbDocumentOut:
    try:
        doc = await kb_svc.create_document(ctx.session, ctx.org.id, payload.title, payload.text)
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(
            f"A knowledge base document titled {payload.title!r} already exists"
        ) from exc
    return KbDocumentOut(id=doc.id, title=doc.title, source=doc.source)


@router.get("/kb/documents/{document_id}", response_model=KbDocumentDetailOut)
async def get_kb_document(
    document_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> KbDocumentDetailOut:
    doc, chunks = await kb_svc.get_document_with_chunks(ctx.session, document_id)
    return KbDocumentDetailOut(
        id=doc.id,
        title=doc.title,
        source=doc.source,
        chunks=[KbChunkOut(seq=c.seq, text=c.text) for c in chunks],
    )


@router.delete("/kb/documents/{document_id}", status_code=204)
async def delete_kb_document(
    document_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> None:
    await kb_svc.delete_document(ctx.session, document_id)
    await ctx.session.commit()
