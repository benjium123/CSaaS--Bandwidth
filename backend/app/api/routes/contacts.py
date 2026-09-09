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
from app.errors import ConflictError, ValidationFailedError
from app.models import (
    CUSTOM_FIELD_KINDS,
    Company,
    Contact,
    ContactNote,
    ContactPhone,
    ContactTag,
    CustomFieldDef,
    Department,
    MessageThread,
    OrgMembership,
    Tag,
)
from app.services import audit as audit_svc
from app.services import contact_visibility
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
    owner_user_id: uuid.UUID | None
    department_id: uuid.UUID | None
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


class ContactOwnerAssignIn(BaseModel):
    owner_user_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None


class ContactBulkAssignIn(BaseModel):
    contact_ids: list[uuid.UUID]
    owner_user_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None


# ----------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------
def _escape_like(s: str) -> str:
    """Escape LIKE metacharacters so a caller-supplied `q` cannot smuggle its own
    wildcards into the pattern (5.9). Backslash first - escaping % and _ before it would
    double-escape a literal backslash already present in the input."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _validate_company(ctx: OrgContext, company_id: uuid.UUID | None) -> None:
    """5.6: company_id was never checked against the org - a stale or cross-org id was
    accepted silently and only failed later (or not at all) wherever it was read back."""
    if company_id is None:
        return
    if await ctx.session.get(Company, company_id) is None:
        raise ValidationFailedError(f"Unknown company id: {company_id}")


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
        owner_user_id=c.owner_user_id,
        department_id=c.department_id,
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
    removed_e164s = [row.e164 for row in existing if row.e164 not in seen]
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

    if removed_e164s:
        # 5.7: a phone removed from a contact left its threads still stamped with THIS
        # contact_id forever - a stale link that never resolved to the right contact
        # (or to none) again, even after the number was reassigned elsewhere.
        await ctx.session.execute(
            sa.update(MessageThread)
            .where(
                MessageThread.contact_e164.in_(removed_e164s),
                MessageThread.contact_id == contact.id,
            )
            .values(contact_id=None)
        )

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
    scope: str | None = Query(None),
) -> list[ContactOut]:
    vis_scope = await contact_visibility.resolve_scope(
        ctx.session,
        ctx.org,
        user_id=ctx.actor_user_id,
        permissions=ctx.role.permissions or [],
    )
    stmt = sa.select(Contact).order_by(Contact.display_name.asc(), Contact.id.asc())
    predicate = contact_visibility.visible_contacts_filter(vis_scope)
    if predicate is not None:
        stmt = stmt.where(predicate)

    if scope is not None:
        if scope == "mine":
            if ctx.actor_user_id is None:
                return []
            stmt = stmt.where(Contact.owner_user_id == ctx.actor_user_id)
        elif scope == "team":
            if not vis_scope.department_ids:
                return []
            stmt = stmt.where(Contact.department_id.in_(vis_scope.department_ids))
        elif scope == "unowned":
            stmt = stmt.where(
                Contact.owner_user_id.is_(None), Contact.department_id.is_(None)
            )
        else:
            raise ValidationFailedError("scope must be mine, team or unowned")

    if q:
        needle = f"%{_escape_like(q.strip().lower())}%"
        phone_match = sa.select(ContactPhone.contact_id).where(
            sa.func.lower(ContactPhone.e164).like(needle, escape="\\")
        )
        stmt = stmt.where(
            sa.or_(
                sa.func.lower(Contact.display_name).like(needle, escape="\\"),
                Contact.id.in_(phone_match),
            )
        )
    rows = (await ctx.session.execute(stmt.limit(limit))).scalars().all()

    # 5.8: batch-load every contact's phones in ONE query instead of _out's per-contact
    # query (the classic N+1 - this list route is exactly where it hurt: page size many).
    ids = [c.id for c in rows]
    phones_by_contact: dict[uuid.UUID, list[PhoneOut]] = {}
    if ids:
        phone_rows = (
            await ctx.session.execute(
                sa.select(ContactPhone).where(ContactPhone.contact_id.in_(ids))
            )
        ).scalars().all()
        for p in phone_rows:
            phones_by_contact.setdefault(p.contact_id, []).append(
                PhoneOut(id=p.id, e164=p.e164, label=p.label, is_primary=p.is_primary)
            )
    return [
        ContactOut(
            id=c.id,
            display_name=c.display_name,
            first_name=c.first_name,
            last_name=c.last_name,
            company_id=c.company_id,
            owner_user_id=c.owner_user_id,
            department_id=c.department_id,
            attributes=c.attributes or {},
            phones=phones_by_contact.get(c.id, []),
            created_at=c.created_at,
        )
        for c in rows
    ]


@router.post("/contacts", response_model=ContactOut, status_code=201)
async def create_contact(
    payload: ContactIn,
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:write"))],
) -> ContactOut:
    await _validate_company(ctx, payload.company_id)
    attributes = await svc.validate_attributes(ctx.session, payload.attributes)
    owner_user_id, department_id = await contact_visibility.default_ownership_for_creator(ctx)
    contact = Contact(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        display_name=payload.display_name.strip(),
        first_name=payload.first_name,
        last_name=payload.last_name,
        company_id=payload.company_id,
        owner_user_id=owner_user_id,
        department_id=department_id,
        attributes=attributes,
    )
    ctx.session.add(contact)
    await ctx.session.flush()
    await _sync_phones(ctx, contact, payload.phones)
    await ctx.session.commit()
    return await _out(ctx, contact)


@router.post("/contacts/bulk/assign", response_model=dict[str, int])
async def bulk_assign_contacts(
    payload: ContactBulkAssignIn,
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:assign"))],
) -> dict[str, int]:
    if len(payload.contact_ids) > 500:
        raise ValidationFailedError("You can assign at most 500 contacts at a time")
    if not payload.contact_ids:
        raise ValidationFailedError("Select at least one contact to assign")

    requested = list(dict.fromkeys(payload.contact_ids))
    if len(requested) > 500:
        raise ValidationFailedError("You can assign at most 500 contacts at a time")

    updates = payload.model_dump(exclude_unset=True)
    if "owner_user_id" in updates and updates["owner_user_id"] is not None:
        owner_row = (
            await ctx.session.execute(
                sa.select(OrgMembership).where(OrgMembership.user_id == updates["owner_user_id"])
            )
        ).scalar_one_or_none()
        if owner_row is None:
            raise ValidationFailedError("That person is not a member of this workspace")

    if "department_id" in updates and updates["department_id"] is not None:
        if await ctx.session.get(Department, updates["department_id"]) is None:
            raise ValidationFailedError("That team does not exist")

    vis_scope = await contact_visibility.resolve_scope(
        ctx.session,
        ctx.org,
        user_id=ctx.actor_user_id,
        permissions=ctx.role.permissions or [],
    )
    predicate = contact_visibility.visible_contacts_filter(vis_scope)
    stmt = sa.select(Contact).where(Contact.id.in_(requested))
    if predicate is not None:
        stmt = stmt.where(predicate)
    contacts = (await ctx.session.execute(stmt)).scalars().all()

    updated = 0
    for contact in contacts:
        if "owner_user_id" in updates:
            contact.owner_user_id = updates["owner_user_id"]
        if "department_id" in updates:
            contact.department_id = updates["department_id"]
        updated += 1

    skipped = len(requested) - updated
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="contact.assign",
        target_type="contact",
        target_id=None,
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        detail={"updated": updated, "skipped": skipped},
    )
    await ctx.session.commit()
    return {"updated": updated, "skipped": skipped}


@router.patch("/contacts/{contact_id}/owner", response_model=ContactOut)
async def assign_contact_owner(
    contact_id: uuid.UUID,
    payload: ContactOwnerAssignIn,
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:assign"))],
) -> ContactOut:
    contact = await contact_visibility.get_visible_contact(ctx, contact_id)
    updates = payload.model_dump(exclude_unset=True)

    if "owner_user_id" in updates and updates["owner_user_id"] is not None:
        owner_row = (
            await ctx.session.execute(
                sa.select(OrgMembership).where(OrgMembership.user_id == updates["owner_user_id"])
            )
        ).scalar_one_or_none()
        if owner_row is None:
            raise ValidationFailedError("That person is not a member of this workspace")

    if "department_id" in updates and updates["department_id"] is not None:
        if await ctx.session.get(Department, updates["department_id"]) is None:
            raise ValidationFailedError("That team does not exist")

    new_owner_id = updates.get("owner_user_id", contact.owner_user_id)
    new_department_id = updates.get("department_id", contact.department_id)
    if "owner_user_id" in updates:
        contact.owner_user_id = updates["owner_user_id"]
    if "department_id" in updates:
        contact.department_id = updates["department_id"]

    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="contact.assign",
        target_type="contact",
        target_id=str(contact.id),
        actor_user_id=ctx.actor_user_id,
        actor_api_key_id=ctx.api_key.id if ctx.api_key else None,
        detail={
            "owner_user_id": str(new_owner_id) if new_owner_id is not None else None,
            "department_id": str(new_department_id) if new_department_id is not None else None,
        },
    )
    await ctx.session.commit()
    return await _out(ctx, contact)


@router.get("/contacts/{contact_id}", response_model=ContactOut)
async def get_contact(
    contact_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:read"))],
) -> ContactOut:
    contact = await contact_visibility.get_visible_contact(ctx, contact_id)
    return await _out(ctx, contact)


@router.patch("/contacts/{contact_id}", response_model=ContactOut)
async def patch_contact(
    contact_id: uuid.UUID,
    payload: ContactPatch,
    ctx: Annotated[OrgContext, Depends(require_permission("contacts:write"))],
) -> ContactOut:
    contact = await contact_visibility.get_visible_contact(ctx, contact_id)

    if payload.display_name is not None:
        contact.display_name = payload.display_name.strip()
    if payload.first_name is not None:
        contact.first_name = payload.first_name
    if payload.last_name is not None:
        contact.last_name = payload.last_name
    if payload.company_id is not None:
        await _validate_company(ctx, payload.company_id)
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
    contact = await contact_visibility.get_visible_contact(ctx, contact_id)
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
    await contact_visibility.get_visible_contact(ctx, contact_id)
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
    await contact_visibility.get_visible_contact(ctx, contact_id)
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
    await contact_visibility.get_visible_contact(ctx, contact_id)
    wanted = set(payload.get("tag_ids", []))

    if wanted:
        # 5.5: validate tag ids exist in this org first (mirrors inbox.py::set_labels) -
        # an unknown/cross-org id previously either 500'd on the FK or silently inserted
        # nothing useful.
        found = {
            t.id
            for t in (
                await ctx.session.execute(sa.select(Tag).where(Tag.id.in_(wanted)))
            ).scalars().all()
        }
        missing = wanted - found
        if missing:
            raise ValidationFailedError(f"Unknown tag ids: {sorted(str(m) for m in missing)}")

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
