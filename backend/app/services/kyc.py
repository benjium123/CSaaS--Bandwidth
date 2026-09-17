"""P41 business verification: the application lifecycle.

    draft -> submitted -> in_review <-> needs_info -> approved | rejected
    approved <-> suspended            approved -> reverification_due -> approved

Customers edit only in draft / needs_info. Operators move everything else, and every
operator decision is audited with the operator's user id. Telephony is allowed only in
KYC_TELEPHONY_STATUSES (services/telephony_access.py).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.models import (
    KYC_EDITABLE_STATUSES,
    KYC_ENTITY_TYPES,
    KYC_PERSON_ROLES,
    KycCheck,
    KycDocument,
    KycPerson,
    KycProfile,
    KycStepUp,
    Org,
    OrgMembership,
    Role,
    SecurityAlert,
)
from app.services import audit as audit_svc
from app.services import ban_list, kyc_checks, kyc_risk, sanctions, stripe_client

log = structlog.get_logger(__name__)

#: Bump when the customer agreement text changes; acceptance records the version.
AGREEMENT_VERSION = "2026-09-16"

REQUIRED_BUSINESS_FIELDS = (
    "country",
    "legal_name",
    "entity_type",
    "registration_number",
    "registered_address",
    "website",
    "business_email",
    "business_phone",
)
REQUIRED_USE_CASE_FIELDS = (
    "description",
    "vertical",
    "who_you_contact",
    "list_source",
    "monthly_calls",
    "monthly_texts",
    "destination_countries",
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"submitted"}),
    "submitted": frozenset({"in_review", "needs_info", "approved", "rejected"}),
    "in_review": frozenset({"needs_info", "approved", "rejected"}),
    "needs_info": frozenset({"submitted", "in_review", "rejected"}),
    "approved": frozenset({"suspended", "reverification_due"}),
    "reverification_due": frozenset({"approved", "suspended", "needs_info"}),
    "suspended": frozenset({"approved", "rejected", "reverification_due"}),
    "rejected": frozenset(),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def identity_hash(first_name: str | None, last_name: str | None, dob: str | None) -> str | None:
    if not (first_name or last_name) or not dob:
        return None
    words = " ".join(sorted(sanctions.normalize_name(f"{first_name or ''} {last_name or ''}")))
    return hashlib.sha256(f"{words}|{dob}".encode()).hexdigest()


def transition(profile: KycProfile, new_status: str) -> None:
    current = profile.status or "draft"
    if new_status not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise ConflictError(
            f"An application that is {current.replace('_', ' ')} cannot become "
            f"{new_status.replace('_', ' ')}"
        )
    profile.status = new_status


# --------------------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------------------
async def get_profile(session: AsyncSession, org_id: uuid.UUID) -> KycProfile | None:
    return (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one_or_none()


async def get_or_create_profile(session: AsyncSession, org_id: uuid.UUID) -> KycProfile:
    row = await get_profile(session, org_id)
    if row is None:
        row = KycProfile(id=uuid.uuid4(), org_id=org_id, status="draft")
        session.add(row)
        await session.flush()
    return row


def _require_editable(profile: KycProfile) -> None:
    if profile.status not in KYC_EDITABLE_STATUSES:
        raise ConflictError(
            "This application can no longer be edited"
            if profile.status != "approved"
            else "Your business is approved; contact support to change these details"
        )


def update_business(settings: Settings, profile: KycProfile, data: dict) -> None:
    _require_editable(profile)
    if "country" in data and data["country"] is not None:
        country = str(data["country"]).upper()
        if country not in settings.kyc_country_list:
            from app.config import countries_phrase

            raise ValidationFailedError(
                "We can currently verify businesses registered in "
                f"{countries_phrase(settings.kyc_country_list)} only"
            )
        data["country"] = country
    if data.get("entity_type") is not None and data["entity_type"] not in KYC_ENTITY_TYPES:
        raise ValidationFailedError(f"Business type must be one of {', '.join(KYC_ENTITY_TYPES)}")
    if data.get("incorporation_date") is not None and data["incorporation_date"] > date.today():
        raise ValidationFailedError("The incorporation date cannot be in the future")
    for field in (
        "country",
        "legal_name",
        "dba_name",
        "entity_type",
        "registration_number",
        "tax_id",
        "incorporation_date",
        "registered_address",
        "operating_address",
        "website",
        "business_email",
        "business_phone",
    ):
        if field in data:
            setattr(profile, field, data[field])


#: Changing any of these means the AI's read of the company documents no longer applies.
COMPANY_DOC_FIELDS = ("legal_name", "dba_name", "registration_number", "tax_id", "country")
COMPANY_DOC_KINDS = ("registration_certificate", "articles", "tax_id_letter")


async def reset_document_reviews(
    session: AsyncSession, org_id: uuid.UUID, *, kinds: tuple[str, ...], person_id=None
) -> int:
    """P43: a document reviewed against old details must be read again."""
    stmt = sa.select(KycDocument).where(KycDocument.org_id == org_id, KycDocument.kind.in_(kinds))
    if person_id is not None:
        stmt = stmt.where(KycDocument.person_id == person_id)
    rows = (await session.execute(stmt)).scalars().all()
    for row in rows:
        row.review_result = None
        row.review = None
        row.reviewed_at = None
    return len(rows)


def update_use_case(settings: Settings, profile: KycProfile, data: dict) -> str:
    """Returns "applied" or "pending_review" (a change to an approved business waits for an
    operator and keeps the approved use case in force meanwhile)."""
    cleaned = dict(data)
    cleaned["destination_countries"] = sorted(
        {str(c).upper() for c in (cleaned.get("destination_countries") or []) if str(c).strip()}
    )
    if cleaned.get("vertical"):
        cleaned["vertical"] = str(cleaned["vertical"]).strip().lower()
    if profile.status in KYC_EDITABLE_STATUSES:
        profile.use_case = cleaned
        return "applied"
    if profile.status in ("approved", "reverification_due"):
        profile.use_case_pending = cleaned
        return "pending_review"
    raise ConflictError("This application can no longer be edited")


def accept_agreement(
    profile: KycProfile, *, user_id: uuid.UUID, ip: str | None, version: str
) -> None:
    _require_editable(profile)
    if version != AGREEMENT_VERSION:
        raise ValidationFailedError(
            "The agreement has changed - reload the page and review it again"
        )
    profile.agreement_version = version
    profile.agreement_accepted_at = _now()
    profile.agreement_accepted_by = user_id
    profile.agreement_ip = ip


# --------------------------------------------------------------------------------------
# Persons
# --------------------------------------------------------------------------------------
async def add_person(
    session: AsyncSession,
    profile: KycProfile,
    *,
    role: str,
    full_name: str,
    email: str | None,
    ownership_percent: int | None,
    user_id: uuid.UUID | None,
    residential_address: dict | None = None,
) -> KycPerson:
    if role not in KYC_PERSON_ROLES:
        raise ValidationFailedError(f"Role must be one of {', '.join(KYC_PERSON_ROLES)}")
    if role in ("owner", "beneficial_owner"):
        _require_editable(profile)
    if ownership_percent is not None and not 0 <= ownership_percent <= 100:
        raise ValidationFailedError("Ownership must be between 0 and 100 percent")
    if not full_name.strip():
        raise ValidationFailedError("Enter the person's full legal name")
    row = KycPerson(
        id=uuid.uuid4(),
        org_id=profile.org_id,
        role=role,
        full_name=full_name.strip()[:255],
        email=(email or "").strip().lower() or None,
        ownership_percent=ownership_percent,
        user_id=user_id,
        status="not_started",
        residential_address=residential_address,
    )
    session.add(row)
    return row


async def set_residential_address(
    session: AsyncSession, profile: KycProfile, person: KycPerson, address: dict
) -> None:
    """P43: owners declare where they live now; a proof of address must match it."""
    _require_editable(profile)
    if person.residential_address != address:
        person.residential_address = address
        await reset_document_reviews(
            session, profile.org_id, kinds=("proof_of_address",), person_id=person.id
        )


async def get_person(session: AsyncSession, org_id: uuid.UUID, person_id: uuid.UUID) -> KycPerson:
    row = await session.get(KycPerson, person_id)
    if row is None or row.org_id != org_id:
        raise NotFoundError("Person not found")
    return row


async def start_person_verification(
    session: AsyncSession,
    settings: Settings,
    person: KycPerson,
    *,
    return_url: str,
    actor_user_id: uuid.UUID | None = None,
) -> str:
    if person.status == "verified" or person.identity_hash is not None:
        profile = await get_profile(session, person.org_id)
        # Annual re-verification is the one time a verified person checks again.
        if profile is None or profile.status not in ("reverification_due", "needs_info"):
            raise ConflictError("This person is already verified")
        # P43: a verified person who has an account re-verifies themselves - nobody else can
        # start (and complete) the check in their place.
        if person.user_id is not None and actor_user_id != person.user_id:
            raise PermissionDeniedError(
                f"Only {person.full_name} can repeat their own ID check", code="not_your_identity"
            )
    created = await stripe_client.create_verification_session(
        settings,
        metadata={
            "purpose": "kyc_person",
            "org_id": str(person.org_id),
            "person_id": str(person.id),
        },
        return_url=return_url,
    )
    person.stripe_verification_session_id = created["id"]
    person.status = "pending"
    person.last_error = None
    return created["url"]


PERSON_STATUS_RANK = {
    "not_started": 0,
    "pending": 1,
    "requires_input": 2,
    "canceled": 2,
    "processing": 3,
    "verified": 4,
}


async def apply_person_outcome(session: AsyncSession, person: KycPerson, outcome: dict) -> bool:
    """Apply a Stripe outcome. Returns False when a RE-verification came back as a different
    person (the previous identity is kept and the person must redo the check)."""
    status = outcome.get("status")
    if person.status == "verified":
        return True  # terminal; a late or replayed event never un-verifies
    if status == "verified":
        first, last = outcome.get("first_name"), outcome.get("last_name")
        new_hash = identity_hash(first, last, outcome.get("dob"))
        if person.identity_hash is not None and new_hash != person.identity_hash:
            person.status = "requires_input"
            person.last_error = "identity_mismatch: this ID belongs to a different person"
            return False
        person.status = "verified"
        person.verified_name = " ".join(p for p in (first, last) if p)[:255] or None
        person.document_type = outcome.get("document_type") or None
        person.document_country = (outcome.get("document_country") or "")[:2].upper() or None
        person.identity_hash = identity_hash(first, last, outcome.get("dob"))
        person.verified_at = _now()
        person.last_error = None
    elif status == "processing":
        if PERSON_STATUS_RANK.get(person.status, 0) < PERSON_STATUS_RANK["processing"]:
            person.status = "processing"
    elif status == "requires_input":
        person.status = "requires_input"
        person.last_error = (outcome.get("error_code") or "Verification needs another try")[:255]
    elif status == "canceled":
        person.status = "canceled"
    return True


async def handle_identity_event(session: AsyncSession, settings: Settings, event: dict) -> None:
    """identity.verification_session.* webhook. The event only says WHICH session changed;
    the outcome is always re-read from Stripe with verified outputs expanded."""
    obj = (event.get("data") or {}).get("object") or {}
    vs_id = obj.get("id")
    metadata = obj.get("metadata") or {}
    if not vs_id:
        return
    outcome = await stripe_client.retrieve_verification_outcome(settings, vs_id)
    purpose = metadata.get("purpose") or outcome["metadata"].get("purpose")

    if purpose == "kyc_person":
        try:
            org_id = uuid.UUID(metadata.get("org_id") or outcome["metadata"].get("org_id"))
        except (TypeError, ValueError):
            log.warning("identity_event_bad_org", vs=vs_id)
            return
        set_org_context(session, org_id)
        person = (
            await session.execute(
                sa.select(KycPerson).where(KycPerson.stripe_verification_session_id == vs_id)
            )
        ).scalar_one_or_none()
        if person is None:
            log.warning("identity_event_unknown_person", vs=vs_id)
            return
        same_person = await apply_person_outcome(session, person, outcome)
        if not same_person:
            session.add(
                SecurityAlert(
                    id=uuid.uuid4(),
                    kind="identity_mismatch",
                    org_id=org_id,
                    user_id=person.user_id,
                    status="open",
                    detail={
                        "person": person.full_name,
                        "role": person.role,
                        "reason": "Re-verification was completed with a different person's ID",
                    },
                )
            )
        elif person.status == "verified":
            # P43: screening must use the REAL identity, not the name typed before the ID
            # check finished.
            profile = await get_profile(session, org_id)
            if profile is not None and profile.status not in ("draft",):
                await rescreen(session, settings, profile)
                await refresh_risk(session, settings, profile)
        audit_svc.record(
            session,
            org_id,
            action="kyc.person_verification",
            target_type="kyc_person",
            target_id=str(person.id),
            detail={"status": person.status},
        )
    elif purpose == "step_up":
        from app.services import kyc_step_up

        await kyc_step_up.apply_outcome(session, vs_id, outcome)
    else:
        log.warning("identity_event_unknown_purpose", vs=vs_id, purpose=purpose)


# --------------------------------------------------------------------------------------
# Submit
# --------------------------------------------------------------------------------------
async def missing_for_submission(session: AsyncSession, profile: KycProfile) -> list[str]:
    missing = [f for f in REQUIRED_BUSINESS_FIELDS if not getattr(profile, f)]
    use_case = profile.use_case or {}
    missing += [
        f"use_case.{f}" for f in REQUIRED_USE_CASE_FIELDS if use_case.get(f) in (None, "", [])
    ]
    persons = await kyc_checks.persons_for(session, profile.org_id)
    if not any(p.role == "owner" for p in persons):
        missing.append("owner")
    unstarted = [
        p.full_name
        for p in persons
        if p.role in ("owner", "beneficial_owner") and p.status in ("not_started", "canceled")
    ]
    if unstarted:
        missing.append("id_verification")
    # P43: every owner declares where they live now and proves it with a recent document.
    owners = [p for p in persons if p.role in ("owner", "beneficial_owner")]
    if any(not p.residential_address for p in owners):
        missing.append("residential_address")
    proven = set(
        (
            await session.execute(
                sa.select(KycDocument.person_id).where(
                    KycDocument.org_id == profile.org_id,
                    KycDocument.kind == "proof_of_address",
                    KycDocument.person_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if any(p.id not in proven for p in owners):
        missing.append("proof_of_address")
    documents = (
        await session.execute(
            # P43: an owner's proof of address is not a business document.
            sa.select(sa.func.count(KycDocument.id)).where(
                KycDocument.org_id == profile.org_id, KycDocument.kind != "proof_of_address"
            )
        )
    ).scalar_one()
    if not documents:
        missing.append("documents")
    if profile.agreement_version != AGREEMENT_VERSION or profile.agreement_accepted_at is None:
        missing.append("agreement")
    return missing


async def submit(
    session: AsyncSession,
    settings: Settings,
    profile: KycProfile,
    *,
    user_id: uuid.UUID,
    flagged_login: bool,
    http_client=None,
) -> None:
    missing = await missing_for_submission(session, profile)
    if missing:
        raise ValidationFailedError(
            "Finish these parts first: " + ", ".join(missing), code="kyc_incomplete"
        )
    transition(profile, "submitted")
    profile.submitted_at = _now()
    profile.submitted_by = user_id
    profile.submitted_from_flagged_login = flagged_login
    profile.info_request = None
    await session.flush()
    await kyc_checks.run_all(session, settings, profile, client=http_client)
    await session.flush()
    await refresh_risk(session, settings, profile)
    audit_svc.record(
        session,
        profile.org_id,
        action="kyc.submitted",
        target_type="kyc_profile",
        target_id=str(profile.id),
        actor_user_id=user_id,
        detail={"risk_tier": profile.risk_tier},
    )


async def refresh_risk(session: AsyncSession, settings: Settings, profile: KycProfile) -> None:
    persons = await kyc_checks.persons_for(session, profile.org_id)
    checks = await kyc_checks.latest_checks(session, profile.org_id)
    tier, reasons = kyc_risk.evaluate(settings, profile, persons, checks)
    profile.risk_tier = tier
    profile.risk_reasons = reasons


# --------------------------------------------------------------------------------------
# Operator decisions
# --------------------------------------------------------------------------------------
async def load_for_operator(session: AsyncSession, org_id: uuid.UUID) -> KycProfile:
    org = await session.get(Org, org_id)
    if org is None:
        raise NotFoundError("Organization not found")
    set_org_context(session, org_id)
    profile = await get_profile(session, org_id)
    if profile is None:
        raise NotFoundError("This organization has not started verification")
    return profile


def _audit_operator(
    session, profile: KycProfile, action: str, operator_id: uuid.UUID, **detail
) -> None:
    audit_svc.record(
        session,
        profile.org_id,
        action=action,
        target_type="kyc_profile",
        target_id=str(profile.id),
        actor_user_id=operator_id,
        detail={"operator_user_id": str(operator_id), **detail},
    )


async def rescreen(session: AsyncSession, settings: Settings, profile: KycProfile) -> None:
    """Re-run the identity-dependent checks against the people as they are NOW."""
    persons = await kyc_checks.persons_for(session, profile.org_id)
    kyc_checks.check_sanctions(session, settings, profile, persons)
    await kyc_checks.check_ban_list(session, profile, persons)
    kyc_checks.check_name_match(session, profile, persons)
    await session.flush()


async def approval_blockers(session: AsyncSession, profile: KycProfile) -> list[str]:
    blockers: list[str] = []
    persons = await kyc_checks.persons_for(session, profile.org_id)
    for p in persons:
        if p.role in ("owner", "beneficial_owner") and p.status != "verified":
            blockers.append(f"ID check not verified for {p.full_name}")
    if not any(p.role == "owner" and p.status == "verified" for p in persons):
        blockers.append("No verified owner")
    checks = await kyc_checks.latest_checks(session, profile.org_id)
    # P43: documents are read by the safety AI (the video call is gone). A high-risk
    # application needs every document to fully match, not just "needs a closer look".
    documents = checks.get("documents")
    high = profile.risk_tier == "high"
    if documents is None or documents.result in ("pending", "error"):
        blockers.append("Documents have not finished their automatic review")
    elif documents.result == "fail":
        blockers.append("Documents don't match the application: " + documents.summary)
    elif high and documents.result != "pass":
        blockers.append("High-risk application: every document must fully match")
    for kind in ("sanctions", "ban_list"):
        check = checks.get(kind)
        if check is None:
            blockers.append(f"The {kind.replace('_', ' ')} check has not run")
        elif check.result == "fail":
            blockers.append(f"The {kind.replace('_', ' ')} check failed")
        elif kind == "sanctions" and check.result == "error":
            blockers.append("Sanctions lists are not loaded - screening did not run")
    name_match = checks.get("name_match")
    if name_match is None or name_match.result != "pass":
        blockers.append("Owner names on the application don't match their verified IDs")
    registry = checks.get("registry")
    if registry is None or registry.result not in ("pass", "warn"):
        blockers.append("Registry check is not complete or did not pass")
    return blockers


async def start_review(session, profile: KycProfile, operator_id: uuid.UUID) -> None:
    transition(profile, "in_review")
    _audit_operator(session, profile, "kyc.review_started", operator_id)


async def request_info(session, profile: KycProfile, operator_id: uuid.UUID, message: str) -> None:
    if not message.strip():
        raise ValidationFailedError("Tell the business what is needed")
    transition(profile, "needs_info")
    profile.info_request = message.strip()
    _audit_operator(session, profile, "kyc.info_requested", operator_id, message=message[:500])


async def approve(
    session: AsyncSession,
    settings: Settings,
    profile: KycProfile,
    operator_id: uuid.UUID,
    note: str,
) -> None:
    await rescreen(session, settings, profile)
    blockers = await approval_blockers(session, profile)
    if blockers:
        await session.commit()  # keep the fresh screening results for the operator
        raise ConflictError(
            "Cannot approve yet: " + "; ".join(blockers), code="kyc_approval_blocked"
        )
    transition(profile, "approved")
    profile.decided_at = _now()
    profile.decided_by = operator_id
    profile.decision_reason = note.strip() or None
    profile.next_reverification_at = _now() + timedelta(days=settings.kyc_reverify_days)
    if profile.use_case_pending:
        profile.use_case = profile.use_case_pending
        profile.use_case_pending = None
    _audit_operator(session, profile, "kyc.approved", operator_id, note=note[:500])


async def reject(
    session: AsyncSession,
    profile: KycProfile,
    operator_id: uuid.UUID,
    reason: str,
    *,
    ban: bool,
) -> int:
    if not reason.strip():
        raise ValidationFailedError("A reason is required")
    transition(profile, "rejected")
    profile.decided_at = _now()
    profile.decided_by = operator_id
    profile.decision_reason = reason.strip()
    banned = await ban_org_identifiers(session, profile, operator_id, reason) if ban else 0
    _audit_operator(
        session, profile, "kyc.rejected", operator_id, reason=reason[:500], banned=banned
    )
    return banned


async def record_video_call(
    session, profile: KycProfile, operator_id: uuid.UUID, note: str
) -> None:
    if profile.status not in ("submitted", "in_review", "needs_info", "reverification_due"):
        raise ConflictError("Video calls are recorded while an application is under review")
    profile.video_call_done_at = _now()
    profile.video_call_by = operator_id
    profile.video_call_note = note.strip() or None
    _audit_operator(session, profile, "kyc.video_call_recorded", operator_id, note=note[:500])


async def record_manual_registry(
    session, profile: KycProfile, operator_id: uuid.UUID, *, result: str, link: str, note: str
) -> KycCheck:
    if result not in ("pass", "fail"):
        raise ValidationFailedError("Result must be pass or fail")
    if not link.strip():
        raise ValidationFailedError("Paste the registry page you checked")
    row = kyc_checks._record(
        session,
        profile,
        "registry",
        result,
        (
            "Operator confirmed the business is registered and active"
            if result == "pass"
            else "Operator could not confirm the registration"
        ),
        {"manual": True, "link": link.strip()[:500], "note": note.strip()[:1000]},
        created_by=operator_id,
    )
    _audit_operator(session, profile, "kyc.registry_recorded", operator_id, result=result)
    return row


async def set_limits(
    session,
    profile: KycProfile,
    operator_id: uuid.UUID,
    *,
    deposit_required_cents: int | None,
    limits: dict | None,
) -> None:
    if deposit_required_cents is not None and deposit_required_cents < 0:
        raise ValidationFailedError("Deposit cannot be negative")
    cleaned = None
    if limits:
        cleaned = {}
        for key in ("daily_calls", "daily_texts", "max_numbers"):
            value = limits.get(key)
            if value is None:
                continue
            if not isinstance(value, int) or value < 0:
                raise ValidationFailedError(f"{key} must be a whole number of 0 or more")
            cleaned[key] = value
        cleaned = cleaned or None
    profile.deposit_required_cents = deposit_required_cents
    profile.limits = cleaned
    _audit_operator(
        session,
        profile,
        "kyc.limits_set",
        operator_id,
        deposit_required_cents=deposit_required_cents,
        limits=cleaned,
    )


async def ban_org_identifiers(
    session: AsyncSession, profile: KycProfile, operator_id: uuid.UUID, reason: str
) -> int:
    persons = await kyc_checks.persons_for(session, profile.org_id)
    identifiers = await kyc_checks.identifiers_for_org(session, profile, persons)
    count = 0
    for kind, value_hash in set(identifiers):
        row = await ban_list.add(
            session,
            kind=kind,
            value_hash=value_hash,
            display_hint=f"from {profile.legal_name or 'org'}"[:64],
            reason=reason,
            source_org_id=profile.org_id,
            created_by=operator_id,
        )
        if row is not None:
            count += 1
    return count


# --------------------------------------------------------------------------------------
# Org creation hook
# --------------------------------------------------------------------------------------
async def unverified_orgs_owned_by(session: AsyncSession, user_id: uuid.UUID) -> int:
    # JUSTIFIED allow_unscoped: counting a user's own not-yet-approved workspaces across
    # every org they own, before any org context exists.
    return (
        await session.execute(
            sa.select(sa.func.count(KycProfile.id))
            .join(OrgMembership, OrgMembership.org_id == KycProfile.org_id)
            .join(Role, Role.id == OrgMembership.role_id)
            .where(
                OrgMembership.user_id == user_id,
                Role.name == "owner",
                KycProfile.status.in_(("draft", "submitted", "in_review", "needs_info")),
            )
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()


async def step_up_rows_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[KycStepUp]:
    return list(
        (
            await session.execute(
                sa.select(KycStepUp)
                .where(KycStepUp.user_id == user_id)
                .order_by(KycStepUp.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
