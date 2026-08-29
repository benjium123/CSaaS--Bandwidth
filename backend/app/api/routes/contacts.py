from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.api.routes.numbers import to_e164
from app.auth.deps import OrgContext, require_permission
from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models import (
    CUSTOM_FIELD_KINDS,
    Company,
    Contact,
    ContactNote,
    ContactPhone,
    ContactTag,
    CustomFieldDef,
    Tag,
)
from app.services import contacts as svc

router = APIRouter(prefix="/api/v1", tags=["contacts"])


# ----------------------------------------------------------------------------------
# Schemas
# ----------------------------------------------------------------------------------
class PhoneIn(BaseModel):
    e164: str
    label: str = "mobile"
    is_primary: bool = False


class PhoneOut(BaseModel):
    id: uuid.UUID
    e164: str
    label: str
    is_primary: bool


class ContactIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=255)
    first_name: str | None = None
    last_name: str | None = None
    company_id: uuid.UUID | None = None
    phones: list[PhoneIn] = []
    attributes: dict = {}


class ContactPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    first_name: str | None = None
    last_name: str | None = None
    company_id: uuid.UUID | None = None
    phones: list[PhoneIn] | None = None
    attributes: dict | None = None


class ContactOut(BaseModel):
    id: uuid.UUID
    display_name: str
    first_name: str | None
    last_name: str | None
    company_id: uuid.UUID | None
    attributes: dict
    phones: list[PhoneOut]
    created_at: datetime


class TagIn(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    color: str = "#64748b"


class TagOut(BaseModel):
    id: uuid.UUID
    name: str
    color: str


class NoteIn(BaseModel):
    body: str = Field(min_length=1)


class CompanyIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    domain: str | None = None


class CustomFieldIn(BaseModel):
    key: str = Field(min_length=1, max_length=63)
    label: str = Field(min_length=1, max_length=127)
    kind: str = "text"
    options: list[str] = []


# ----------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------
async def _phones_of(ctx: OrgContext, contact_id: uuid.UUID) -> list[PhoneOut]:
    rows = (
        await ctx.session.execute(
            sa.select(ContactPhone).where(ContactPhone.contact_id == contact_id)
        )
    ).scalars().all()
    return [
        PhoneOut(id=p.id, e164=p.e164, label=p.label, is_primary=p.is_primary) for p in rows
    ]


async def _out(ctx: OrgContext, c: Contact) -> ContactOut:
    return ContactOut(
        id=c.id,
        display_name=c.display_name,
        first_name=c.first_name,
        last_name=c.last_name,
        company_id=c.company_id,
        attributes=c.attributes or {},
        phones=await _phones_of(ctx, c.id),
        created_at=c.created_at,
    )


async def _sync_phones(ctx: OrgContext, contact: Contact, phones: list[PhoneIn]) -> None:
    """Diff current vs submitted, re-linking threads for every number added."""
    normalized = [(to_e164(p.e164), p.label, p.is_primary) for p in phones]
    seen = {e for e, _, _ in normalized}

    existing = list(
        (
            await ctx.session.execute(
                sa.select(ContactPhone).where(ContactPhone.contact_id == contact.id)
            )
        ).scalars().all()
    )
    for row in existing:
        if row.e164 not in seen:
            await ctx.session.delete(row)
    have = {row.e164: row for row in existing}

    for e164, label, is_primary in normalized:
        if e164 in have:
            have[e164].label = label
            have[e164].is_primary = is_primary
            continue
        ctx.session.add(
            ContactPhone(
                id=uuid.uuid4(),
                org_id=ctx.org.id,
                contact_id=contact.id,
                e164=e164,
                label=label,
                is_primary=is_primary,
            )
        )
    try:
        await ctx.session.flush()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(
            "One of those phone numbers already belongs to another contact in this org"
        ) from exc

    # A contact created AFTER messages already exist is the common real-world order.
    for e164 in seen:
        await svc.link_threads_for_phone(ctx.session, ctx.org.id, e164, contact.id)


# ----------------------------------------------------------------------------------
# Contacts
# ----------------------------------------------------------------------------------
@router.get("/contacts", response_model=list[ContactOut])
async def list_contacts(
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:read"))],
    q: str | None = None,
    limit: int = Query(50, ge=1, le=100),
) -> list[ContactOut]:
    stmt = sa.select(Contact).order_by(Contact.display_name.asc(), Contact.id.asc())
    if q:
        needle = f"%{q.strip().lower()}%"
        phone_match = sa.select(ContactPhone.contact_id).where(
            sa.func.lower(ContactPhone.e164).like(needle)
        )
        stmt = stmt.where(
            sa.or_(
                sa.func.lower(Contact.display_name).like(needle),
                Contact.id.in_(phone_match),
            )
        )
    rows = (await ctx.session.execute(stmt.limit(limit))).scalars().all()
    return [await _out(ctx, c) for c in rows]


@router.post("/contacts", response_model=ContactOut, status_code=201)
async def create_contact(
    payload: ContactIn,
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:write"))],
) -> ContactOut:
    attributes = await svc.validate_attributes(ctx.session, payload.attributes)
    contact = Contact(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        display_name=payload.display_name.strip(),
        first_name=payload.first_name,
        last_name=payload.last_name,
        company_id=payload.company_id,
        attributes=attributes,
    )
    ctx.session.add(contact)
    await ctx.session.flush()
    await _sync_phones(ctx, contact, payload.phones)
    await ctx.session.commit()
    return await _out(ctx, contact)


@router.get("/contacts/{contact_id}", response_model=ContactOut)
async def get_contact(
    contact_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:read"))],
) -> ContactOut:
    contact = await ctx.session.get(Contact, contact_id)
    if contact is None:
        raise NotFoundError("Contact not found")
    return await _out(ctx, contact)


@router.patch("/contacts/{contact_id}", response_model=ContactOut)
async def patch_contact(
    contact_id: uuid.UUID,
    payload: ContactPatch,
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:write"))],
) -> ContactOut:
    contact = await ctx.session.get(Contact, contact_id)
    if contact is None:
        raise NotFoundError("Contact not found")

    if payload.display_name is not None:
        contact.display_name = payload.display_name.strip()
    if payload.first_name is not None:
        contact.first_name = payload.first_name
    if payload.last_name is not None:
        contact.last_name = payload.last_name
    if payload.company_id is not None:
        contact.company_id = payload.company_id
    if payload.attributes is not None:
        contact.attributes = await svc.validate_attributes(ctx.session, payload.attributes)
    if payload.phones is not None:
        await _sync_phones(ctx, contact, payload.phones)

    await ctx.session.commit()
    return await _out(ctx, contact)


@router.delete("/contacts/{contact_id}", status_code=204)
async def delete_contact(
    contact_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:write"))],
) -> None:
    contact = await ctx.session.get(Contact, contact_id)
    if contact is None:
        raise NotFoundError("Contact not found")
    # Threads keep their history: message_threads.contact_id is ON DELETE SET NULL.
    await ctx.session.delete(contact)
    await ctx.session.commit()


# ----------------------------------------------------------------------------------
# Notes / tags
# ----------------------------------------------------------------------------------
@router.get("/contacts/{contact_id}/notes")
async def list_notes(
    contact_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:read"))],
) -> list[dict]:
    rows = (
        await ctx.session.execute(
            sa.select(ContactNote)
            .where(ContactNote.contact_id == contact_id)
            .order_by(ContactNote.created_at.desc())
        )
    ).scalars().all()
    return [
        {
            "id": n.id,
            "body": n.body,
            "author_user_id": n.author_user_id,
            "created_at": n.created_at,
        }
        for n in rows
    ]


@router.post("/contacts/{contact_id}/notes", status_code=201)
async def add_note(
    contact_id: uuid.UUID,
    payload: NoteIn,
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:write"))],
) -> dict:
    if await ctx.session.get(Contact, contact_id) is None:
        raise NotFoundError("Contact not found")
    note = ContactNote(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        contact_id=contact_id,
        author_user_id=ctx.actor_user_id,
        body=payload.body,
    )
    ctx.session.add(note)
    await ctx.session.commit()
    return {"id": note.id, "body": note.body, "created_at": note.created_at}


@router.put("/contacts/{contact_id}/tags")
async def set_contact_tags(
    contact_id: uuid.UUID,
    payload: dict[str, list[uuid.UUID]],
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:write"))],
) -> dict:
    if await ctx.session.get(Contact, contact_id) is None:
        raise NotFoundError("Contact not found")
    wanted = set(payload.get("tag_ids", []))

    existing = list(
        (
            await ctx.session.execute(
                sa.select(ContactTag).where(ContactTag.contact_id == contact_id)
            )
        ).scalars().all()
    )
    for row in existing:
        if row.tag_id not in wanted:
            await ctx.session.delete(row)
    have = {row.tag_id for row in existing}
    for tag_id in wanted - have:
        ctx.session.add(
            ContactTag(
                id=uuid.uuid4(), org_id=ctx.org.id, contact_id=contact_id, tag_id=tag_id
            )
        )
    await ctx.session.commit()
    return {"contact_id": contact_id, "tag_ids": sorted(str(t) for t in wanted)}


@router.get("/tags", response_model=list[TagOut])
async def list_tags(
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:read"))],
) -> list[TagOut]:
    rows = (await ctx.session.execute(sa.select(Tag).order_by(Tag.name))).scalars().all()
    return [TagOut(id=t.id, name=t.name, color=t.color) for t in rows]


@router.post("/tags", response_model=TagOut, status_code=201)
async def create_tag(
    payload: TagIn,
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:write"))],
) -> TagOut:
    tag = Tag(id=uuid.uuid4(), org_id=ctx.org.id, name=payload.name.strip(), color=payload.color)
    ctx.session.add(tag)
    try:
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(f"A tag named {payload.name!r} already exists") from exc
    return TagOut(id=tag.id, name=tag.name, color=tag.color)


# ----------------------------------------------------------------------------------
# Companies
# ----------------------------------------------------------------------------------
@router.get("/companies")
async def list_companies(
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:read"))],
) -> list[dict]:
    rows = (
        await ctx.session.execute(sa.select(Company).order_by(Company.name))
    ).scalars().all()
    return [{"id": c.id, "name": c.name, "domain": c.domain} for c in rows]


@router.post("/companies", status_code=201)
async def create_company(
    payload: CompanyIn,
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:write"))],
) -> dict:
    company = Company(
        id=uuid.uuid4(), org_id=ctx.org.id, name=payload.name.strip(), domain=payload.domain
    )
    ctx.session.add(company)
    await ctx.session.commit()
    return {"id": company.id, "name": company.name, "domain": company.domain}


# ----------------------------------------------------------------------------------
# Custom field definitions
# ----------------------------------------------------------------------------------
@router.get("/custom-fields")
async def list_custom_fields(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> list[dict]:
    rows = (
        await ctx.session.execute(sa.select(CustomFieldDef).order_by(CustomFieldDef.key))
    ).scalars().all()
    return [
        {"id": d.id, "key": d.key, "label": d.label, "kind": d.kind, "options": d.options}
        for d in rows
    ]


@router.post("/custom-fields", status_code=201)
async def create_custom_field(
    payload: CustomFieldIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> dict[str, Any]:
    svc.validate_field_key(payload.key)
    if payload.kind not in CUSTOM_FIELD_KINDS:
        raise ValidationFailedError(f"kind must be one of: {', '.join(CUSTOM_FIELD_KINDS)}")
    if payload.kind == "select" and not payload.options:
        raise ValidationFailedError("select fields need at least one option")

    definition = CustomFieldDef(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        key=payload.key,
        label=payload.label,
        kind=payload.kind,
        options=payload.options,
    )
    ctx.session.add(definition)
    try:
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(f"A custom field with key {payload.key!r} already exists") from exc
    return {
        "id": definition.id,
        "key": definition.key,
        "label": definition.label,
        "kind": definition.kind,
        "options": definition.options,
    }
