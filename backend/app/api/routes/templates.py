from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.auth.deps import OrgContext, require_permission
from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models import Contact, MessageTemplate
from app.services import templates as tmpl

router = APIRouter(prefix="/api/v1/templates", tags=["templates"])


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=127)
    body: str = Field(min_length=1)
    media_asset_ids: list[uuid.UUID] = []


class TemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    body: str
    media_asset_ids: list[uuid.UUID]
    tokens: list[str]


class RenderIn(BaseModel):
    contact_id: uuid.UUID


def _out(t: MessageTemplate) -> TemplateOut:
    return TemplateOut(
        id=t.id,
        name=t.name,
        body=t.body,
        media_asset_ids=[uuid.UUID(str(m)) for m in (t.media_asset_ids or [])],
        tokens=tmpl.extract_tokens(t.body),
    )


@router.get("", response_model=list[TemplateOut])
async def list_templates(
    ctx: Annotated[OrgContext, Depends(require_permission("templates:read"))],
) -> list[TemplateOut]:
    rows = (
        await ctx.session.execute(sa.select(MessageTemplate).order_by(MessageTemplate.name))
    ).scalars().all()
    return [_out(t) for t in rows]


@router.post("", response_model=TemplateOut, status_code=201)
async def create_template(
    payload: TemplateIn,
    ctx: Annotated[OrgContext, Depends(require_permission("templates:manage"))],
) -> TemplateOut:
    # Validate tokens at WRITE time too, so a broken template cannot be saved and then
    # surprise an operator mid-conversation.
    for token in tmpl.extract_tokens(payload.body):
        root = token.split(".")[0]
        if root not in tmpl.ALLOWED_ROOTS:
            raise ValidationFailedError(
                f"Unknown merge field {{{{{token}}}}}. Allowed roots: "
                f"{', '.join(sorted(tmpl.ALLOWED_ROOTS))}"
            )

    row = MessageTemplate(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        name=payload.name.strip(),
        body=payload.body,
        media_asset_ids=[str(m) for m in payload.media_asset_ids],
    )
    ctx.session.add(row)
    try:
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(f"A template named {payload.name!r} already exists") from exc
    return _out(row)


@router.post("/{template_id}/render")
async def render_template(
    template_id: uuid.UUID,
    payload: RenderIn,
    ctx: Annotated[OrgContext, Depends(require_permission("templates:read"))],
) -> dict:
    row = await ctx.session.get(MessageTemplate, template_id)
    if row is None:
        raise NotFoundError("Template not found")
    contact = await ctx.session.get(Contact, payload.contact_id)
    if contact is None:
        raise NotFoundError("Contact not found")

    namespace = {
        "contact": {
            "first_name": contact.first_name or "",
            "last_name": contact.last_name or "",
            "display_name": contact.display_name or "",
            "attributes": contact.attributes or {},
        },
        "org": {"name": ctx.org.name},
    }
    try:
        result = tmpl.render(row.body, namespace)
    except tmpl.UnknownTokenError as exc:
        # Fail LOUD at render rather than sending "{{contact.frist_name}}" to a customer.
        raise ValidationFailedError(f"Unknown merge field: {exc.token}") from exc

    return {
        "body": result.body,
        "media_asset_ids": row.media_asset_ids or [],
        "warnings": result.warnings,
    }
