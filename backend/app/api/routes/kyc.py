"""P41 business verification - the customer side.

The business application is OWNER-ONLY, so every route that reads or edits it now requires
``require_owner``. Operators review applications through routes/ops.py, never through these
routes. The owner is the person who signs the application, so the owner is the person
the platform is verifying.

There is ONE deliberate exception, and it is the whole reason ``/me/verify``,
``/persons/{person_id}/verify`` and ``/step-up`` stay open. Both
``deps.py::IDENTITY_GATED_PERMISSIONS``
and ``_require_verified_privileged_member`` refuse a NON-owner who has not completed their own
ID + selfie check, so a non-owner admin locked out of every privileged permission would have
NO path forward if their only route to that check were also owner-only. ``/me/verify`` and the
step-up endpoints are that path, and locking them would be a permanent, self-inflicted lockout
of the workspace's own admins. ``/persons/{person_id}/verify`` stays reachable for the same
reason: ``services/kyc.py::start_person_verification`` already raises ``not_your_identity`` when
the actor is not the person, so that self-check - not the role - is the boundary.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

import sqlalchemy as sa
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Request,
    Response,
    UploadFile,
)
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import (
    OrgContext,
    current_identity_session,
    get_current_user,
    require_owner,
    require_permission,
)
from app.config import Settings, get_active_settings
from app.db.session import get_session
from app.errors import PermissionDeniedError, ValidationFailedError
from app.models import KycDocument, KycPerson, KycStepUp, SecurityAlert, User
from app.services import identity as identity_svc
from app.services import kyc as kyc_svc
from app.services import kyc_checks, kyc_doc_reader, kyc_documents, kyc_step_up

router = APIRouter(prefix="/api/v1/kyc", tags=["kyc"])

#: Checks a customer sees: the outcome only. Sanctions/ban-list/AI details are for operators.
CUSTOMER_VISIBLE_CHECKS = ("registry", "website", "email_domain", "name_match", "documents")


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
    #: P43: where an owner lives now (proven by a proof_of_address document).
    residential_address: AddressIn | None = None


class ApplicationIn(BaseModel):
    legal_name: str = Field(min_length=2, max_length=255)
    country: str = Field(min_length=2, max_length=2)
    phone: str = Field(min_length=5, max_length=32)
    industry: str = Field(default="", max_length=64)
    purpose: str = Field(default="", max_length=4000)
    customer_country: str = Field(default="", max_length=2)
    accept_personal_agreement: bool = False


@router.put("/application")
async def save_application(
    payload: ApplicationIn,
    ctx: Annotated[OrgContext, Depends(require_owner)],
) -> dict:
    """Save personal verification without replacing a company's own application."""
    import phonenumbers

    if ctx.membership is None:
        raise PermissionDeniedError("Sign in as the account owner to verify")
    country = payload.country.upper()
    customer_country = payload.customer_country.upper()
    countries = phonenumbers.SUPPORTED_REGIONS | {"AQ", "BV", "HM", "GS", "TF", "UM", "PN"}
    if country not in countries or (customer_country and customer_country not in countries):
        raise ValidationFailedError("Select a valid country")
    if not payload.legal_name.strip():
        raise ValidationFailedError("Enter your legal name")
    try:
        phone = phonenumbers.parse(payload.phone, None)
        if not payload.phone.startswith("+") or not phonenumbers.is_possible_number(phone):
            raise ValueError()
    except (phonenumbers.NumberParseException, ValueError):
        raise ValidationFailedError("Enter a phone number with its country prefix") from None
    # Serialize owner creation when two tabs save a new application together.
    await ctx.session.execute(
        sa.select(type(ctx.org)).where(type(ctx.org).id == ctx.org.id).with_for_update()
    )
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    kyc_svc._require_editable(profile)
    user = await ctx.session.get(User, ctx.membership.user_id)
    details = {
        **payload.model_dump(),
        "legal_name": payload.legal_name.strip(),
        "country": country,
        "customer_country": customer_country,
        "phone": phonenumbers.format_number(phone, phonenumbers.PhoneNumberFormat.E164),
    }
    personal = ctx.org.account_type == "individual"
    if personal:
        profile.legal_name = details["legal_name"]
        if profile.country != country:
            from app.services import phone_region

            phone_region.forget(ctx.org.id)
        profile.country = country
        profile.business_phone = details["phone"]
        profile.business_email = user.email
        profile.use_case = {
            "application_version": 2,
            "vertical": payload.industry.strip(),
            "description": payload.purpose.strip(),
            "destination_countries": [customer_country] if customer_country else [],
        }
    else:
        details["user_id"] = str(user.id)
        if payload.accept_personal_agreement:
            details["agreement_version"] = kyc_svc.AGREEMENT_VERSION
            details["agreement_accepted_at"] = kyc_svc._now().isoformat()
            details["agreement_accepted_by"] = str(user.id)
        profile.use_case = {**(profile.use_case or {}), "applicant_details": details}
    people = await kyc_checks.persons_for(ctx.session, ctx.org.id)
    owner = next((p for p in people if p.user_id == user.id and p.role == "owner"), None)
    if owner is None:
        await kyc_svc.add_person(
            ctx.session,
            profile,
            role="owner",
            full_name=details["legal_name"],
            email=user.email,
            ownership_percent=None,
            user_id=user.id,
        )
    elif owner.status in ("not_started", "canceled", "requires_input"):
        owner.full_name = details["legal_name"]
    await ctx.session.commit()
    return await _profile_out(ctx.session, profile, _viewer(ctx), owner=True)


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


def _is_owner(ctx) -> bool:
    """Owner = the wildcard role, which is how every other gate in this codebase asks."""
    from app.models.rbac import WILDCARD

    return WILDCARD in (ctx.role.permissions or [])


def _viewer(ctx) -> uuid.UUID | None:
    """The signed-in member, or None for an API key. Only used to answer "is this person
    you?" - an API key is nobody, and gets `is_you: false` on every person."""
    return ctx.membership.user_id if ctx.membership is not None else None


def _person_out(p, viewer_id: uuid.UUID | None = None) -> dict:
    return {
        "id": str(p.id),
        "role": p.role,
        "full_name": p.full_name,
        "email": p.email,
        "ownership_percent": p.ownership_percent,
        "is_user": p.user_id is not None,
        # WHETHER this person is the viewer, never WHICH user they are. The console has to
        # answer "is this me?" - only the person themselves may start their own ID check
        # (`kyc.py::start_person_verification` raises `not_your_identity` otherwise), so a
        # re-verification prompt that offers everyone a button offers most people a dead
        # end. Matching on email would be the alternative and it is wrong: a person added
        # by an owner can carry a different address from the one their account signs in
        # with. Exposing the raw `user_id` to everyone holding `org:read` would answer the
        # question and hand out the workspace's user ids as a side effect.
        "is_you": viewer_id is not None and p.user_id == viewer_id,
        "status": p.status,
        "verified_name": p.verified_name,
        "document_country": p.document_country,
        "identity_provider": getattr(p, "identity_provider", None),
        "verified_at": p.verified_at.isoformat() if p.verified_at else None,
        "last_error": p.last_error,
        "residential_address": p.residential_address,
    }


def _document_out(d: KycDocument) -> dict:
    return {
        "id": str(d.id),
        "kind": d.kind,
        "filename": d.filename,
        "content_type": d.content_type,
        "size_bytes": d.size_bytes,
        "uploaded_at": d.created_at.isoformat() if d.created_at else None,
        "person_id": str(d.person_id) if d.person_id else None,
        # P43: the applicant sees whether the automatic review accepted it, and why not.
        "review_status": ("reviewing" if d.review_result in (None, "error") else d.review_result),
        "review_message": kyc_doc_reader.customer_message(d),
    }


async def _profile_out(
    session: AsyncSession,
    profile,
    viewer_id: uuid.UUID | None = None,
    *,
    owner: bool = False,
) -> dict:
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
        "account_type": (
            "individual" if await kyc_svc._is_individual(session, profile.org_id) else "business"
        ),
        # P43: the countries a business can verify from (KYC_COUNTRIES).
        "supported_countries": get_active_settings().kyc_country_list,
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
        "persons": [_person_out(p, viewer_id) for p in persons],
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
        # OWNERS ONLY, and the asymmetry is deliberate rather than an oversight being tidied
        # up. The suspension mail already carries this reason, so nothing is being withheld -
        # but that mail goes to the OWNERS, while this payload is behind `org:read`, which is
        # every member. A compliance suspension can name an individual, and the member most
        # likely to be reading it is the one it is about. `decision_reason` is not a precedent:
        # a rejected org is pre-approval with a couple of people in it, a suspended org is a
        # live business with staff.
        "suspension_reason": (
            profile.suspension_reason if profile.status == "suspended" and owner else None
        ),
        "limits": profile.limits,
        "deposit_required_cents": profile.deposit_required_cents,
        "next_reverification_at": (
            profile.next_reverification_at.isoformat() if profile.next_reverification_at else None
        ),
    }


@router.get("/profile")
async def get_profile(ctx: Annotated[OrgContext, Depends(require_owner)]) -> dict:
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    out = await _profile_out(ctx.session, profile, _viewer(ctx), owner=_is_owner(ctx))
    await ctx.session.commit()
    return out


@router.put("/profile/business")
async def put_business(
    payload: BusinessIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_owner)],
    background: BackgroundTasks,
) -> dict:
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    data = payload.model_dump(exclude_unset=True, mode="python")
    if await kyc_svc._is_individual(ctx.session, ctx.org.id):
        company_only = (
            "dba_name",
            "entity_type",
            "registration_number",
            "tax_id",
            "incorporation_date",
            "registered_address",
            "operating_address",
            "website",
        )
        for key in company_only:
            value = data.get(key)
            if value not in (None, ""):
                raise ValidationFailedError("That field is for business accounts only")
    for key in ("registered_address", "operating_address"):
        if isinstance(data.get(key), dict):
            data[key]["country"] = data[key]["country"].upper()
    before = {f: getattr(profile, f) for f in kyc_svc.COMPANY_DOC_FIELDS}
    kyc_svc.update_business(request.app.state.settings, profile, data)
    if any(getattr(profile, f) != before[f] for f in kyc_svc.COMPANY_DOC_FIELDS):
        await kyc_svc.reset_document_reviews(
            ctx.session, ctx.org.id, kinds=kyc_svc.COMPANY_DOC_KINDS
        )
        background.add_task(_run_automation, request.app, ctx.org.id, True)
    await ctx.session.commit()
    return await _profile_out(ctx.session, profile, _viewer(ctx), owner=_is_owner(ctx))


@router.put("/profile/use-case")
async def put_use_case(
    payload: UseCaseIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_owner)],
) -> dict:
    from app.auth.deps import check_step_up

    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    if await kyc_svc._is_individual(ctx.session, ctx.org.id):
        if payload.monthly_texts != 0:
            raise ValidationFailedError("Texting is not available on individual accounts")
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
    out = await _profile_out(ctx.session, profile, _viewer(ctx), owner=_is_owner(ctx))
    return {"result": result, **out}


@router.post("/persons", status_code=201)
async def add_person(
    payload: PersonIn,
    ctx: Annotated[OrgContext, Depends(require_owner)],
) -> dict:
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    if await kyc_svc._is_individual(ctx.session, ctx.org.id):
        if payload.role != "owner" or not payload.is_me:
            raise ValidationFailedError("An individual account has exactly one owner: you")
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
        residential_address=(
            _address_dict(payload.residential_address) if payload.residential_address else None
        ),
    )
    await ctx.session.commit()
    return _person_out(person)


def _address_dict(address: AddressIn) -> dict:
    data = address.model_dump()
    data["country"] = data["country"].upper()
    return data


@router.put("/persons/{person_id}/address")
async def set_person_address(
    person_id: uuid.UUID,
    payload: AddressIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_owner)],
    background: BackgroundTasks,
) -> dict:
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    person = await kyc_svc.get_person(ctx.session, ctx.org.id, person_id)
    await kyc_svc.set_residential_address(ctx.session, profile, person, _address_dict(payload))
    background.add_task(_run_automation, request.app, ctx.org.id, True)
    await ctx.session.commit()
    return _person_out(person)


@router.delete("/persons/{person_id}", status_code=204)
async def delete_person(
    person_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_owner)],
) -> Response:
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    person = await kyc_svc.get_person(ctx.session, ctx.org.id, person_id)
    if await kyc_svc._is_individual(ctx.session, ctx.org.id) and getattr(
        person, "identity_hash", None
    ):
        raise ValidationFailedError("The verified owner of an individual account cannot be removed")
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
        return_url=_return_url(settings, payload.return_url, "/verification"),
        actor_user_id=ctx.actor_user_id,
    )
    await ctx.session.commit()
    return {"url": url, "status": person.status}


@router.post("/documents", status_code=201)
async def upload_document(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_owner)],
    kind: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    background: BackgroundTasks,
    person_id: Annotated[uuid.UUID | None, Form()] = None,
) -> dict:
    settings: Settings = request.app.state.settings
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    if profile.status not in ("draft", "needs_info"):
        raise ValidationFailedError("Documents can be added while the application is open")
    # P43: a proof of address belongs to one owner.
    if kind == "proof_of_address":
        if person_id is None:
            raise ValidationFailedError("Choose which owner this proof of address is for")
        owner = await kyc_svc.get_person(ctx.session, ctx.org.id, person_id)
        if owner.role not in ("owner", "beneficial_owner"):
            raise ValidationFailedError("Proof of address is needed for owners only")
    elif person_id is not None:
        raise ValidationFailedError("Only a proof of address is linked to a person")
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
    doc.person_id = person_id
    await ctx.session.commit()
    # Read it straight away so the applicant learns within seconds if it won't be accepted.
    background.add_task(_run_automation, request.app, ctx.org.id, True)
    return _document_out(doc)


async def _run_automation(app, org_id: uuid.UUID, reviews_only: bool) -> None:
    from app.db.session import get_sessionmaker
    from app.services import kyc_automation

    try:
        async with get_sessionmaker()() as session:
            await kyc_automation.process(
                session,
                app.state.settings,
                app.state.media_store,
                org_id,
                http_client=getattr(app.state, "kyc_http_client", None),
                reviews_only=reviews_only,
            )
    except kyc_automation.AIDown:
        import structlog

        # expected during an AI outage: the documents are marked and the sweeper retries
        structlog.get_logger("kyc").warning("kyc_automation_background_ai_down", org_id=str(org_id))
    except Exception:  # noqa: BLE001 - the sweeper retries anything left pending
        import structlog

        structlog.get_logger("kyc").exception("kyc_automation_background_failed")


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_owner)],
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
    ctx: Annotated[OrgContext, Depends(require_owner)],
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
    return await _profile_out(ctx.session, profile, _viewer(ctx), owner=_is_owner(ctx))


@router.post("/submit")
async def submit(
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_owner)],
    background: BackgroundTasks,
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
    # P43: documents, registry fallback, risk and the AI decision pack - no human needed.
    background.add_task(_run_automation, request.app, ctx.org.id, False)
    return await _profile_out(ctx.session, profile, _viewer(ctx), owner=_is_owner(ctx))


@router.post("/me/verify")
async def verify_me(
    payload: VerifyIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_permission("org:read"))],
) -> dict:
    """ID + selfie for the signed-in member (admins and billing staff of an approved
    business, who need it before using those powers).

    NOT owner-only, and that is the point: this is the ONE route a non-owner member has to
    satisfy ``_require_verified_privileged_member``. Locking it to the owner would lock
    every non-owner admin out of every privileged permission with no way forward.
    """
    if ctx.membership is None:
        raise ValidationFailedError("API keys cannot be identity-verified")
    settings: Settings = request.app.state.settings
    individual = await kyc_svc._is_individual(ctx.session, ctx.org.id)
    profile = await kyc_svc.get_or_create_profile(ctx.session, ctx.org.id)
    person = (
        await ctx.session.execute(
            sa.select(KycPerson).where(
                KycPerson.org_id == ctx.org.id, KycPerson.user_id == ctx.membership.user_id
            )
        )
    ).scalar_one_or_none()
    if individual and ctx.role.name != "owner":
        raise PermissionDeniedError("Only the account owner can verify an individual account")
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
            role="owner" if individual else role,
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
        return_url=_return_url(settings, payload.return_url, "/verification"),
        actor_user_id=ctx.actor_user_id,
    )
    await ctx.session.commit()
    return {"url": url, "status": person.status}


class LimitRequestIn(BaseModel):
    message: str = Field(min_length=10, max_length=2000)


@router.post("/limit-request", status_code=202)
async def request_higher_limits(
    payload: LimitRequestIn,
    request: Request,
    ctx: Annotated[OrgContext, Depends(require_owner)],
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
