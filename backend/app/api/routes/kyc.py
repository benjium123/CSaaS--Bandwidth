"""P41 business verification - the customer side.

Reading needs ``org:read``; changing the application needs ``org:update`` (owner and admin).
Operators review applications through routes/ops.py, never through these routes.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import (
    OrgContext,
    current_identity_session,
    get_current_user,
    require_permission,
)
from app.config import Settings
from app.db.session import get_session
from app.errors import PermissionDeniedError, ValidationFailedError
from app.models import KycDocument, KycPerson, KycStepUp, SecurityAlert, User
from app.services import identity as identity_svc
from app.services import kyc as kyc_svc
from app.services import kyc_checks, kyc_documents, kyc_step_up

router = APIRouter(prefix="/api/v1/kyc", tags=["kyc"])

#: Checks a customer sees: the outcome only. Sanctions/ban-list/AI details are for operators.
CUSTOMER_VISIBLE_CHECKS = ("registry", "website", "email_domain", "name_match")


class AddressIn(BaseModel):
    line1: str = Field(min_length=1, max_length=255)
    line2: str | None = Field(default=None, max_length=255)
    city: str = Field(min_length=1, max_length=128)
    region: str | None = Field(default=None, max_length=128)
    postal_code: str = Field(min_length=1, max_length=32)
    country: str = Field(min_length=2, max_length=2)


class BusinessIn(BaseModel):
    country: str | None = Field(default=None, min_length=2, max_length=2)
    legal_name: str | None = Field(default=None, max_length=255)
    dba_name: str | None = Field(default=None, max_length=255)
    entity_type: str | None = None
    registration_number: str | None = Field(default=None, max_length=64)
    tax_id: str | None = Field(default=None, max_length=64)
    incorporation_date: date | None = None
    registered_address: AddressIn | None = None
    operating_address: AddressIn | None = None
    website: str | None = Field(default=None, max_length=255)
    business_email: EmailStr | None = None
    business_phone: str | None = Field(default=None, max_length=32)


class UseCaseIn(BaseModel):
    description: str = Field(min_length=20, max_length=4000)
    vertical: str = Field(min_length=2, max_length=64)
    who_you_contact: str = Field(min_length=5, max_length=2000)
    list_source: str = Field(min_length=5, max_length=2000)
    monthly_calls: int = Field(ge=0, le=100_000_000)
    monthly_texts: int = Field(ge=0, le=100_000_000)
    destination_countries: list[str] = Field(min_length=1, max_length=50)
    sample_script: str | None = Field(default=None, max_length=4000)


class PersonIn(BaseModel):
    role: str
    full_name: str = Field(min_length=2, max_length=255)
    email: EmailStr | None = None
    ownership_percent: int | None = None
    #: True = this person is the signed-in user.
    is_me: bool = False


class AgreementIn(BaseModel):
    version: str
    accept: bool


class VerifyIn(BaseModel):
    return_url: str | None = Field(default=None, max_length=500)


class StepUpIn(BaseModel):
    action: str
    return_url: str | None = Field(default=None, max_length=500)


def _return_url(settings: Settings, requested: str | None, fallback_path: str) -> str:
    """Only ever redirect back to our own console - never to a caller-supplied host."""
    base = settings.public_web_url.rstrip("/")
    if requested and requested.startswith(base + "/"):
        return requested
    return base + fallback_path


def _person_out(p) -> dict:
    return {
        "id": str(p.id),
        "role": p.role,
        "full_name": p.full_name,
        "email": p.email,
        "ownership_percent": p.ownership_percent,
        "is_user": p.user_id is not None,
        "status": p.status,
        "verified_name": p.verified_name,
        "document_country": p.document_country,
        "verified_at": p.verified_at.isoformat() if p.verified_at else None,
        "last_error": p.last_error,
    }


def _document_out(d: KycDocument) -> dict:
    return {
        "id": str(d.id),
        "kind": d.kind,
        "filename": d.filename,
        "content_type": d.content_type,
        "size_bytes": d.size_bytes,
        "uploaded_at": d.created_at.isoformat() if d.created_at else None,
    }


async def _profile_out(session: AsyncSession, profile) -> dict:
    persons = await kyc_checks.persons_for(session, profile.org_id)
    documents = (
        (
            await session.execute(
                sa.select(KycDocument)
                .where(KycDocument.org_id == profile.org_id)
                .order_by(KycDocument.created_at)
            )
        )
        .scalars()
        .all()
    )
    checks = await kyc_checks.latest_checks(session, profile.org_id)
    return {
        "status": profile.status,
        "business": {
            "country": profile.country,
            "legal_name": profile.legal_name,
            "dba_name": profile.dba_name,
            "entity_type": profile.entity_type,
            "registration_number": profile.registration_number,
            "tax_id": profile.tax_id,
            "incorporation_date": (
                profile.incorporation_date.isoformat() if profile.incorporation_date else None
            ),
            "registered_address": profile.registered_address,
            "operating_address": profile.operating_address,
            "website": profile.website,
            "business_email": profile.business_email,
            "business_phone": profile.business_phone,
        },
        "use_case": profile.use_case,
        "use_case_pending": profile.use_case_pending,
        "persons": [_person_out(p) for p in persons],
        "documents": [_document_out(d) for d in documents],
        "checks": {
            k: {"result": c.result, "summary": c.summary}
            for k, c in checks.items()
            if k in CUSTOMER_VISIBLE_CHECKS
        },
        "agreement": {
            "current_version": kyc_svc.AGREEMENT_VERSION,
            "accepted_version": profile.agreement_version,
            "accepted_at": (
                profile.agreement_accepted_at.isoformat() if profile.agreement_accepted_at else None
            ),
        },
        "missing": (
            await kyc_svc.missing_for_submission(session, profile)
            if profile.status in ("draft", "needs_info")
            else []
        ),
        "info_request": profile.info_request if profile.status == "needs_info" else None,
        "submitted_at": profile.submitted_at.isoformat() if profile.submitted_at else None,
        "decided_at": profile.decided_at.isoformat() if profile.decided_at else None,
        "decision_reason": profile.decision_reason if profile.status == "rejected" else None,
        "limits": profile.limits,
        "deposit_required_cents": profile.deposit_required_cents,
        "next_reverification_at": (
            profile.next_reverification_at.isoformat() if profile.next_reverification_at else None
        ),
    }


@router.get("/profile")
async def get_profile(ctx: Annotated[OrgContext, Depends(require_permission("org:read"))]) -> dict:
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    out = await _profile_out(ctx.session, profile)
    await ctx.session.commit()
    return out


@router.put("/profile/business")
async def put_business(
    payload: BusinessIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> dict:
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    data = payload.model_dump(exclude_unset=True, mode="python")
    for key in ("registered_address", "operating_address"):
        if isinstance(data.get(key), dict):
            data[key]["country"] = data[key]["country"].upper()
    kyc_svc.update_business(request.app.state.settings, profile, data)
    await ctx.session.commit()
    return await _profile_out(ctx.session, profile)


@router.put("/profile/use-case")
async def put_use_case(
    payload: UseCaseIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> dict:
    from app.auth.deps import check_step_up

    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    if profile.status in ("approved", "reverification_due"):
        if ctx.membership is None:
            raise PermissionDeniedError("API keys cannot change the declared use case")
        user = await ctx.session.get(User, ctx.membership.user_id)
        await check_step_up(
            request, ctx.session, user, kind="recent_selfie", action="use_case_change"
        )
    result = kyc_svc.update_use_case(
        request.app.state.settings, profile, payload.model_dump(mode="python")
    )
    await ctx.session.commit()
    return {"result": result, **(await _profile_out(ctx.session, profile))}


@router.post("/persons", status_code=201)
async def add_person(
    payload: PersonIn,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> dict:
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    user_id = None
    if payload.is_me:
        if ctx.membership is None:
            raise ValidationFailedError("API keys cannot add themselves as a person")
        user_id = ctx.membership.user_id
    person = await kyc_svc.add_person(
        ctx.session,
        profile,
        role=payload.role,
        full_name=payload.full_name,
        email=payload.email,
        ownership_percent=payload.ownership_percent,
        user_id=user_id,
    )
    await ctx.session.commit()
    return _person_out(person)


@router.delete("/persons/{person_id}", status_code=204)
async def delete_person(
    person_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> Response:
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    person = await kyc_svc.get_person(ctx.session, ctx.org.id, person_id)
    if person.status == "verified" or profile.status not in ("draft", "needs_info"):
        raise ValidationFailedError("A verified person, or one on a submitted application, stays")
    await ctx.session.delete(person)
    await ctx.session.commit()
    return Response(status_code=204)


@router.post("/persons/{person_id}/verify")
async def start_person_verification(
    person_id: uuid.UUID,
    payload: VerifyIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> dict:
    """Returns Stripe's hosted ID + selfie page. Open it yourself when the person is you,
    or send the link to the owner it belongs to."""
    settings: Settings = request.app.state.settings
    person = await kyc_svc.get_person(ctx.session, ctx.org.id, person_id)
    url = await kyc_svc.start_person_verification(
        ctx.session,
        settings,
        person,
        return_url=_return_url(settings, payload.return_url, "/settings/verification"),
    )
    await ctx.session.commit()
    return {"url": url, "status": person.status}


@router.post("/documents", status_code=201)
async def upload_document(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
    kind: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> dict:
    settings: Settings = request.app.state.settings
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    if profile.status not in ("draft", "needs_info"):
        raise ValidationFailedError("Documents can be added while the application is open")
    data = await file.read(settings.kyc_document_max_bytes + 1)
    doc = await kyc_documents.store(
        ctx.session,
        settings,
        request.app.state.media_store,
        org_id=ctx.org.id,
        kind=kind,
        filename=file.filename,
        data=data,
        uploaded_by=ctx.actor_user_id,
    )
    await ctx.session.commit()
    return _document_out(doc)


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> Response:
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    if profile.status not in ("draft", "needs_info"):
        raise ValidationFailedError("Documents on a submitted application cannot be removed")
    doc = await kyc_documents.get(ctx.session, ctx.org.id, document_id)
    await kyc_documents.delete(ctx.session, request.app.state.media_store, doc)
    await ctx.session.commit()
    return Response(status_code=204)


@router.post("/agreement")
async def accept_agreement(
    payload: AgreementIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> dict:
    if not payload.accept:
        raise ValidationFailedError("You must accept the agreement to continue")
    if ctx.membership is None:
        raise ValidationFailedError("A person, not an API key, must accept the agreement")
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    kyc_svc.accept_agreement(
        profile,
        user_id=ctx.membership.user_id,
        ip=identity_svc.client_ip(request),
        version=payload.version,
    )
    await ctx.session.commit()
    return await _profile_out(ctx.session, profile)


@router.post("/submit")
async def submit(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> dict:
    if ctx.membership is None:
        raise ValidationFailedError("A person, not an API key, must submit the application")
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    live = await current_identity_session(request, ctx.session)
    flagged = bool(live is not None and live.risk_flags)
    await kyc_svc.submit(
        ctx.session,
        request.app.state.settings,
        profile,
        user_id=ctx.membership.user_id,
        flagged_login=flagged,
        http_client=getattr(request.app.state, "kyc_http_client", None),
    )
    await ctx.session.commit()
    return await _profile_out(ctx.session, profile)


@router.post("/me/verify")
async def verify_me(
    payload: VerifyIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("org:read"))],
) -> dict:
    """ID + selfie for the signed-in member (admins and billing staff of an approved
    business, who need it before using those powers)."""
    if ctx.membership is None:
        raise ValidationFailedError("API keys cannot be identity-verified")
    settings: Settings = request.app.state.settings
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    person = (
        await ctx.session.execute(
            sa.select(KycPerson).where(
                KycPerson.org_id == ctx.org.id, KycPerson.user_id == ctx.membership.user_id
            )
        )
    ).scalar_one_or_none()
    if person is None:
        user = await ctx.session.get(User, ctx.membership.user_id)
        role = (
            "billing"
            if ctx.role.grants("org:billing") and not ctx.role.grants("members:update")
            else "admin"
        )
        person = await kyc_svc.add_person(
            ctx.session,
            profile,
            role=role,
            full_name=user.full_name or user.email,
            email=user.email,
            ownership_percent=None,
            user_id=user.id,
        )
        await ctx.session.flush()
    url = await kyc_svc.start_person_verification(
        ctx.session,
        settings,
        person,
        return_url=_return_url(settings, payload.return_url, "/settings/verification"),
    )
    await ctx.session.commit()
    return {"url": url, "status": person.status}


class LimitRequestIn(BaseModel):
    message: str = Field(min_length=10, max_length=2000)


@router.post("/limit-request", status_code=202)
async def request_higher_limits(
    payload: LimitRequestIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("org:update"))],
) -> dict:
    """Asks the operators for higher limits. Needs a fresh selfie: raising limits is what a
    hijacked or resold account asks for first."""
    from app.auth.deps import check_org_selfie_step_up

    await check_org_selfie_step_up(request, ctx, action="limit_increase")
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    ctx.session.add(
        SecurityAlert(
            id=uuid.uuid4(),
            kind="limit_request",
            user_id=ctx.actor_user_id,
            org_id=ctx.org.id,
            status="open",
            detail={
                "message": payload.message,
                "current_limits": profile.limits,
                "legal_name": profile.legal_name,
            },
        )
    )
    await ctx.session.commit()
    return {"status": "received"}


# --------------------------------------------------------------------------------------
# Selfie step-up (user-level, not org-scoped)
# --------------------------------------------------------------------------------------
@router.post("/step-up")
async def start_step_up(
    payload: StepUpIn,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    settings: Settings = request.app.state.settings
    row, url = await kyc_step_up.start(
        session,
        settings,
        user,
        action=payload.action,
        return_url=_return_url(settings, payload.return_url, "/"),
    )
    await session.commit()
    return {"id": str(row.id), "url": url, "status": row.status}


@router.get("/step-up/{step_up_id}")
async def get_step_up(
    step_up_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    row = await session.get(KycStepUp, step_up_id)
    if row is None or row.user_id != user.id:
        raise ValidationFailedError("Unknown verification")
    return {
        "id": str(row.id),
        "action": row.action,
        "status": row.status,
        "error": row.last_error,
        "verified_at": row.verified_at.isoformat()
        if isinstance(row.verified_at, datetime)
        else None,
    }
