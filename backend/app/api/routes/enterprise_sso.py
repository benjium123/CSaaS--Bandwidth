"""P42: workspace-side setup for enterprise SSO - verified email domains and SCIM tokens.

Both are standing trust decisions (who may sign in as whom, and who may create and remove
people), so API keys can never make them and SCIM tokens need a fresh second factor plus
the owner's ID step-up.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.auth.deps import (
    OrgContext,
    check_org_selfie_step_up,
    check_step_up,
    require_permission,
)
from app.errors import NotFoundError, PermissionDeniedError
from app.models import OrgDomain, ScimToken, User
from app.services import audit as audit_svc
from app.services import saml as saml_svc
from app.services import sso_provisioning

router = APIRouter(prefix="/api/v1/orgs/current", tags=["identity"])

SCIM_TOKEN_PREFIX = "scim"


class DomainIn(BaseModel):
    domain: str = Field(min_length=3, max_length=253)


class DomainOut(BaseModel):
    id: uuid.UUID
    domain: str
    verified: bool
    verified_at: datetime | None
    last_checked_at: datetime | None
    txt_name: str
    txt_value: str


class ScimTokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=127)


class ScimTokenOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ScimTokenCreatedOut(ScimTokenOut):
    token: str
    base_url: str


class SamlSetupOut(BaseModel):
    sp_entity_id: str
    acs_url: str
    metadata_url: str
    start_url: str


def _domain_out(row: OrgDomain) -> DomainOut:
    record = sso_provisioning.txt_record(row)
    return DomainOut(
        id=row.id,
        domain=row.domain,
        verified=row.verified_at is not None,
        verified_at=row.verified_at,
        last_checked_at=row.last_checked_at,
        txt_name=record["name"],
        txt_value=record["value"],
    )


def _token_out(row: ScimToken) -> ScimTokenOut:
    return ScimTokenOut(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


def _require_person(ctx: OrgContext) -> uuid.UUID:
    if ctx.actor_user_id is None:
        raise PermissionDeniedError("This change needs a signed-in person, not an API key")
    return ctx.actor_user_id


async def _require_recent_2fa(request: Request, ctx: OrgContext, action: str) -> None:
    user = await ctx.session.get(User, _require_person(ctx))
    await check_step_up(request, ctx.session, user, kind="recent_2fa", action=action)


# --- verified domains -----------------------------------------------------------------


@router.get("/domains", response_model=list[DomainOut])
async def list_domains(
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> list[DomainOut]:
    rows = (
        (
            await ctx.session.execute(
                sa.select(OrgDomain)
                .where(OrgDomain.org_id == ctx.org.id)
                .order_by(OrgDomain.domain)
            )
        )
        .scalars()
        .all()
    )
    return [_domain_out(row) for row in rows]


@router.post("/domains", response_model=DomainOut, status_code=201)
async def add_domain(
    payload: DomainIn,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> DomainOut:
    actor = _require_person(ctx)
    row = await sso_provisioning.add_domain(
        ctx.session, ctx.org.id, payload.domain, created_by=actor
    )
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="domain.added",
        target_type="domain",
        target_id=str(row.id),
        actor_user_id=actor,
        detail={"domain": row.domain},
    )
    await ctx.session.commit()
    return _domain_out(row)


async def _get_domain(ctx: OrgContext, domain_id: uuid.UUID) -> OrgDomain:
    row = (
        await ctx.session.execute(
            sa.select(OrgDomain).where(OrgDomain.id == domain_id, OrgDomain.org_id == ctx.org.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Domain not found")
    return row


@router.post("/domains/{domain_id}/verify", response_model=DomainOut)
async def verify_domain(
    domain_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> DomainOut:
    actor = _require_person(ctx)
    row = await _get_domain(ctx, domain_id)
    was_verified = row.verified_at is not None
    client = getattr(request.app.state, "doh_client", None)
    ok = await sso_provisioning.verify_domain(
        ctx.session, request.app.state.settings, row, client=client
    )
    if ok and not was_verified:
        audit_svc.record(
            ctx.session,
            ctx.org.id,
            action="domain.verified",
            target_type="domain",
            target_id=str(row.id),
            actor_user_id=actor,
            detail={"domain": row.domain},
        )
    await ctx.session.commit()
    return _domain_out(row)


@router.delete("/domains/{domain_id}", status_code=204)
async def delete_domain(
    domain_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:write"))],
) -> None:
    await _require_recent_2fa(request, ctx, "domain_remove")
    row = await _get_domain(ctx, domain_id)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="domain.removed",
        target_type="domain",
        target_id=str(row.id),
        actor_user_id=ctx.actor_user_id,
        detail={"domain": row.domain},
    )
    await ctx.session.delete(row)
    await ctx.session.commit()


# --- SAML setup values -------------------------------------------------------------------


@router.get("/sso/saml", response_model=SamlSetupOut)
async def saml_setup(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("settings:read"))],
) -> SamlSetupOut:
    settings = request.app.state.settings
    base = (settings.public_base_url or "").rstrip("/")
    return SamlSetupOut(
        sp_entity_id=saml_svc.sp_entity_id(settings, ctx.org),
        acs_url=saml_svc.acs_url(settings, ctx.org),
        metadata_url=saml_svc.sp_entity_id(settings, ctx.org),
        start_url=f"{base}/api/v1/auth/saml/{ctx.org.slug}/start",
    )


# --- SCIM tokens --------------------------------------------------------------------------


def hash_scim_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@router.get("/scim-tokens", response_model=list[ScimTokenOut])
async def list_scim_tokens(
    ctx: Annotated[OrgContext, Depends(require_permission("members:update"))],
) -> list[ScimTokenOut]:
    rows = (
        (
            await ctx.session.execute(
                sa.select(ScimToken)
                .where(ScimToken.org_id == ctx.org.id)
                .order_by(ScimToken.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_token_out(row) for row in rows]


@router.post("/scim-tokens", response_model=ScimTokenCreatedOut, status_code=201)
async def create_scim_token(
    payload: ScimTokenIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("*"))],
) -> ScimTokenCreatedOut:
    # A SCIM token can add and remove every member: owner only, fresh 2FA, and the owner's
    # ID check (a no-op until KYC is enforced).
    await _require_recent_2fa(request, ctx, "scim_token_create")
    await check_org_selfie_step_up(request, ctx, action="api_key_create")
    prefix = secrets.token_hex(4)
    token = f"{SCIM_TOKEN_PREFIX}_{prefix}_{secrets.token_urlsafe(32)}"
    row = ScimToken(
        id=uuid.uuid4(),
        org_id=ctx.org.id,
        name=payload.name.strip(),
        prefix=prefix,
        token_hash=hash_scim_token(token),
        created_by=ctx.actor_user_id,
    )
    ctx.session.add(row)
    audit_svc.record(
        ctx.session,
        ctx.org.id,
        action="scim_token.created",
        target_type="scim_token",
        target_id=str(row.id),
        actor_user_id=ctx.actor_user_id,
        detail={"name": row.name, "prefix": prefix},
    )
    await ctx.session.commit()
    await ctx.session.refresh(row)
    base = (request.app.state.settings.public_base_url or "").rstrip("/")
    return ScimTokenCreatedOut(
        **_token_out(row).model_dump(), token=token, base_url=f"{base}/scim/v2"
    )


@router.delete("/scim-tokens/{token_id}", status_code=204)
async def revoke_scim_token(
    token_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("members:update"))],
) -> None:
    _require_person(ctx)
    row = (
        await ctx.session.execute(
            sa.select(ScimToken).where(ScimToken.id == token_id, ScimToken.org_id == ctx.org.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("SCIM token not found")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        audit_svc.record(
            ctx.session,
            ctx.org.id,
            action="scim_token.revoked",
            target_type="scim_token",
            target_id=str(row.id),
            actor_user_id=ctx.actor_user_id,
            detail={"prefix": row.prefix},
        )
    await ctx.session.commit()
