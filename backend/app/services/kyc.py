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
from datetime import datetime, timedelta, timezone

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
from app.services import (
    ban_list,
    didit_client,
    identity_provider,
    individual_kyc,
    kyc_checks,
    kyc_risk,
    sanctions,
    stripe_client,
)

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


#: Account type that is verified as a person rather than a business.
ACCOUNT_TYPE_INDIVIDUAL = individual_kyc.ACCOUNT_TYPE_INDIVIDUAL


#: Profile statuses in which a verified person may run the identity check again.
#: reverification_due/needs_info cover the annual re-check; suspended covers an
#: individual whose account was suspended and who must re-prove identity before an
#: operator can unsuspend (approve).
REVERIFICATION_PROFILE_STATUSES = ("reverification_due", "needs_info", "suspended")


#: Prefix of KycPerson.last_error set when a previously verified identity expires.
#: A person carrying it must not be revived by a late event from the same session;
#: only start_person_verification (which clears last_error and assigns a new session)
#: may move them forward again.
IDENTITY_EXPIRED_PREFIX = "identity_expired:"

#: The message stored on the person and shown to the customer when their identity
#: expires. Kept as one constant so the prefix and the text can never drift apart.
IDENTITY_EXPIRED_ERROR = "identity_expired: start a new identity verification"

#: The operator-facing request written to the profile when an expiry forces a fresh
#: ID check and a human review.
IDENTITY_EXPIRED_INFO_REQUEST = (
    "Your identity verification has expired. Start a new ID check and an operator "
    "will review it before your account can be approved again."
)


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


async def _is_individual(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """True when the workspace is an individual account (delegates to individual_kyc)."""
    return await individual_kyc.is_individual(session, org_id)


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
        if country != profile.country:
            # Bare national numbers are parsed in this country from now on.
            from app.services import phone_region

            phone_region.forget(profile.org_id)
        data["country"] = country
    if data.get("entity_type") is not None and data["entity_type"] not in KYC_ENTITY_TYPES:
        raise ValidationFailedError(f"Business type must be one of {', '.join(KYC_ENTITY_TYPES)}")
    # UTC, like kyc_risk: a local date would refuse (or accept) a valid date for part of
    # every day depending on the server's timezone.
    if (
        data.get("incorporation_date") is not None
        and data["incorporation_date"] > datetime.now(timezone.utc).date()
    ):
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
    # Company use-case edits must preserve the separately saved applicant form.
    if (profile.use_case or {}).get("applicant_details"):
        cleaned["applicant_details"] = dict(profile.use_case["applicant_details"])
        if str(cleaned.get("business_description") or "").strip():
            cleaned["applicant_details"]["application_version"] = 4
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
    applicant = (profile.use_case or {}).get("applicant_details")
    if applicant and applicant.get("user_id") == str(user_id):
        profile.use_case = {
            **profile.use_case,
            "applicant_details": {
                **applicant,
                "agreement_version": version,
                "agreement_accepted_at": profile.agreement_accepted_at.isoformat(),
                "agreement_accepted_by": str(user_id),
            },
        }


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
    if await _is_individual(session, profile.org_id):
        await individual_kyc.validate_person_creation(session, profile, role, user_id)
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
    individual = await _is_individual(session, person.org_id)
    if individual:
        # An individual's identity IS the account: only the bound user may ever start
        # (or repeat) the check, and the person must be linked to a user account.
        if person.user_id is None or actor_user_id != person.user_id:
            raise PermissionDeniedError(
                f"Only {person.full_name} can start their own ID check",
                code="not_your_identity",
            )
    if person.status == "verified" or person.identity_hash is not None:
        profile = await get_profile(session, person.org_id)
        # Annual re-verification is the one time a verified person checks again. For an
        # individual whose account is suspended, a fresh identity is required before an
        # operator can unsuspend (approve), so suspended is allowed too - but only for
        # individuals; a suspended business still cannot retry here.
        allowed_statuses = REVERIFICATION_PROFILE_STATUSES
        if profile is None or profile.status not in allowed_statuses:
            raise ConflictError("This person is already verified")
        if profile.status == "suspended" and not individual:
            raise ConflictError("This person is already verified")
        # P43: a verified person who has an account re-verifies themselves - nobody else can
        # start (and complete) the check in their place.
        if person.user_id is not None and actor_user_id != person.user_id:
            raise PermissionDeniedError(
                f"Only {person.full_name} can repeat their own ID check", code="not_your_identity"
            )
    # P44: which provider runs the check is configuration. Everything above this line -
    # including the not_your_identity refusal - happens before any provider is touched, so
    # the rule holds identically whichever provider is active. Individual accounts are
    # always verified with Didit, regardless of the configured default.
    profile = await get_profile(session, person.org_id)
    if individual or (profile and (profile.use_case or {}).get("applicant_details")):
        provider = identity_provider.DiditIdentityProvider()
    else:
        provider = identity_provider.get_provider(settings)
    started = await provider.start(
        settings,
        org_id=person.org_id,
        person_id=person.id,
        email=person.email,
        return_url=return_url,
    )
    if started.provider == "stripe":
        # The Stripe column stays authoritative for Stripe sessions: handle_identity_event
        # looks the person up by it, and rows created before P44 have only this column.
        person.stripe_verification_session_id = started.session_id
    person.identity_provider = started.provider
    person.provider_session_id = started.session_id
    person.status = "pending"
    person.last_error = None
    return started.url


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


async def _apply_person_event(
    session: AsyncSession, settings: Settings, org_id: uuid.UUID, person: KycPerson, outcome: dict
) -> None:
    """Apply one provider outcome to a person and everything that follows from it.

    Shared by the Stripe and the Didit webhook handlers so the two can never drift on the
    identity-mismatch alert, the re-screening or the audit record - those are KYC policy,
    not provider detail.
    """
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
        await _apply_person_event(session, settings, org_id, person, outcome)
    elif purpose == "step_up":
        from app.services import kyc_step_up

        await kyc_step_up.apply_outcome(session, vs_id, outcome)
    else:
        log.warning("identity_event_unknown_purpose", vs=vs_id, purpose=purpose)


async def _didit_person(session: AsyncSession, payload: dict) -> KycPerson | None:
    """Find the person a Didit webhook belongs to, or None.

    The session id is the primary key into our rows. ``vendor_data`` (our KycPerson id) is
    only a fallback for the window where a webhook overtakes the write that stored the
    session id, and it is accepted ONLY when that person has no other session recorded -
    otherwise a webhook could steer an outcome onto a person whose session it does not own.

    A non-empty STRING session id is required: a missing, blank, or non-string value must
    never be coerced into an identifier that could match a row or fall back onto a vendor
    id and steer an outcome onto a person whose session it does not own.
    """
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    session_id = session_id.strip()
    person = (
        await session.execute(
            sa.select(KycPerson).where(
                KycPerson.provider_session_id == session_id,
                KycPerson.identity_provider == "didit",
            )
        )
    ).scalar_one_or_none()
    if person is not None:
        return person
    try:
        vendor_id = uuid.UUID(str(payload.get("vendor_data")))
    except (TypeError, ValueError):
        return None
    candidate = await session.get(KycPerson, vendor_id)
    if candidate is None:
        return None
    # A row that already belongs to another provider (e.g. Stripe) must never be steered
    # by a Didit webhook. A row with no provider recorded is the race seam: the webhook
    # overtook the write that stored the session id, so the fallback is allowed and the
    # binding is stored by the caller.
    if candidate.identity_provider not in (None, "didit"):
        log.warning("didit_event_provider_mismatch", session=session_id)
        return None
    if candidate.provider_session_id not in (None, session_id):
        log.warning("didit_event_session_mismatch", session=session_id)
        return None
    return candidate


async def _didit_expire_person(
    session: AsyncSession, settings: Settings, org_id: uuid.UUID, person: KycPerson
) -> None:
    """A previously verified identity has expired (Didit status "Kyc Expired").

    The person is canceled and must start a NEW identity verification; the verified
    identity (hash, name, document, user binding) is retained so the same human can be
    recognised again. The profile is moved to needs_info so an operator reviews the new
    check before the account can be approved again. This never approves anyone.
    """
    person.status = "canceled"
    person.last_error = IDENTITY_EXPIRED_ERROR

    profile = await get_profile(session, org_id)
    if profile is not None and person.role in ("owner", "beneficial_owner"):
        current = profile.status or "draft"
        if current == "approved":
            # approved -> reverification_due -> needs_info, so the transition table is
            # respected and the profile ends in the state that asks for a new check.
            transition(profile, "reverification_due")
            transition(profile, "needs_info")
        elif current in ("reverification_due", "submitted", "in_review"):
            transition(profile, "needs_info")
        # draft stays draft, suspended stays suspended, needs_info and rejected are
        # unchanged: an expiry must not silently move a profile out of those states.
        if profile.status == "needs_info":
            profile.info_request = IDENTITY_EXPIRED_INFO_REQUEST

    audit_svc.record(
        session,
        org_id,
        action="kyc.person_verification",
        target_type="kyc_person",
        target_id=str(person.id),
        detail={"status": "canceled", "provider": "didit", "reason": "identity_expired"},
    )


async def handle_didit_event(session: AsyncSession, settings: Settings, payload: dict) -> None:
    """Apply one verified Didit webhook payload. The caller has already authenticated the
    signature, the timestamp and the event id; this function only decides what it means."""
    outcome = didit_client.outcome_from_payload(payload)
    if outcome is None:
        # A status we do not map (including "Not Started" and anything unknown) is not a
        # transition we are willing to invent, so the person is left exactly as they are.
        log.warning("didit_event_ignored_status", status=str(payload.get("status")))
        return
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        # A malformed metadata block (a string, a list, None) must not raise on .get and
        # must not be treated as a valid correlation.
        log.warning("didit_event_bad_metadata", session=str(payload.get("session_id")))
        return
    try:
        org_id = uuid.UUID(str(metadata.get("org_id")))
    except (TypeError, ValueError):
        log.warning("didit_event_bad_org", session=str(payload.get("session_id")))
        return
    set_org_context(session, org_id)
    person = await _didit_person(session, payload)
    if person is None or person.org_id != org_id:
        log.warning("didit_event_unknown_person", session=str(payload.get("session_id")))
        return

    # The person is now validated: _didit_person has enforced the allowed provider and
    # refused any conflicting session. Bind the exact session BEFORE any early return so
    # a fallback row (provider recorded but no session, or neither) is correlated to this
    # session for the expiry sentinel and for later pending callbacks. An existing
    # conflicting session was already refused above, so this only fills what is missing.
    session_id = str(payload.get("session_id"))
    if person.identity_provider is None or person.provider_session_id is None:
        person.identity_provider = "didit"
        person.provider_session_id = session_id

    # A person whose identity expired must not be revived by a late event from the same
    # session. Only start_person_verification clears last_error and assigns a new session,
    # so any non-expiry event is ignored until then. The explicit Kyc Expired transition
    # below is the one documented exception.
    if (
        person.last_error is not None
        and person.last_error.startswith(IDENTITY_EXPIRED_PREFIX)
        and payload.get("status") != "Kyc Expired"
    ):
        log.warning("didit_event_ignored_expired_person", session=session_id)
        return

    if payload.get("status") == "Kyc Expired":
        # A previously verified identity is no longer valid. This is a KYC expiration,
        # not an ordinary Expired/Abandoned outcome, so it is handled before the generic
        # mapping and never approves anyone.
        await _didit_expire_person(session, settings, org_id, person)
        return

    if outcome["status"] == "pending":
        # "Awaiting User" only says the person has not finished yet. It may arrive after a
        # later event, so it ratchets forward from not_started and never drags a person
        # back out of processing or verified.
        if PERSON_STATUS_RANK.get(person.status, 0) < PERSON_STATUS_RANK["pending"]:
            person.status = "pending"
        return

    if (
        outcome["status"] == "verified"
        and identity_hash(
            outcome.get("first_name"),
            outcome.get("last_name"),
            outcome.get("dob"),
        )
        is None
        and not (person.status == "verified" and person.identity_hash is not None)
    ):
        # Initial missing identity details park for retry; a repeat
        # verification with a prior hash keeps processing.
        person.status = "processing" if person.identity_hash is not None else "requires_input"
        person.last_error = "identity_unconfirmed: identity details are incomplete"
        audit_svc.record(
            session,
            org_id,
            action="kyc.person_verification",
            target_type="kyc_person",
            target_id=str(person.id),
            detail={"status": person.status, "provider": "didit"},
        )
        return

    # Legacy repair: an authenticated, complete same-session event may arrive
    # for a person that was previously marked verified without an identity
    # hash. Demote to processing so the event below can populate the missing
    # hash. This is intentionally narrow and never touches other statuses.
    if (
        outcome["status"] == "verified"
        and person.status == "verified"
        and person.identity_hash is None
    ):
        person.status = "processing"
    await _apply_person_event(session, settings, org_id, person, outcome)


# --------------------------------------------------------------------------------------
# Submit
# --------------------------------------------------------------------------------------
async def missing_for_submission(session: AsyncSession, profile: KycProfile) -> list[str]:
    if await _is_individual(session, profile.org_id):
        return await individual_kyc.missing_for_submission(session, profile)
    missing = [f for f in REQUIRED_BUSINESS_FIELDS if not getattr(profile, f)]
    use_case = profile.use_case or {}
    missing += [
        f"use_case.{f}" for f in REQUIRED_USE_CASE_FIELDS if use_case.get(f) in (None, "", [])
    ]
    persons = await kyc_checks.persons_for(session, profile.org_id)
    applicant = use_case.get("applicant_details")
    if applicant:
        if (
            applicant.get("application_version") == 3
            and not str(applicant.get("business_description") or "").strip()
        ):
            missing.append("applicant.business_description")
        fields = (
            ("legal_name", "country", "phone")
            if applicant.get("application_version") == 4
            else ("legal_name", "country", "phone", "industry", "purpose", "customer_country")
        )
        if (
            applicant.get("application_version") == 4
            and not str(use_case.get("business_description") or "").strip()
        ):
            missing.append("use_case.business_description")
        for field in fields:
            if not str(applicant.get(field) or "").strip():
                missing.append(f"applicant.{field}")
        if (
            applicant.get("application_version") != 4
            and applicant.get("agreement_version") != AGREEMENT_VERSION
        ):
            missing.append("applicant.agreement")
        own = next(
            (
                p
                for p in persons
                if str(p.user_id) == applicant.get("user_id") and p.role == "owner"
            ),
            None,
        )
        if (
            own is None
            or own.status != "verified"
            or own.identity_provider != "didit"
            or not own.identity_hash
            or not kyc_checks.names_match(applicant.get("legal_name", ""), own.verified_name or "")
        ):
            missing.append("id_verification")
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
    if await _is_individual(session, profile.org_id):
        issues = await individual_kyc.validate_owner(session, profile, user_id)
        if issues:
            raise ValidationFailedError(
                "Finish these parts first: " + ", ".join(issues), code="kyc_incomplete"
            )
    missing = await missing_for_submission(session, profile)
    if missing:
        raise ValidationFailedError(
            "Finish these parts first: " + ", ".join(missing), code="kyc_incomplete"
        )
    persons = await kyc_checks.persons_for(session, profile.org_id)
    identifiers = await kyc_checks.identifiers_for_org(session, profile, persons)
    if await ban_list.matches(session, identifiers):
        raise ValidationFailedError(
            "This application matches a blocked account. Contact support.",
            code="account_blacklisted",
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
    account_type = (
        ACCOUNT_TYPE_INDIVIDUAL if await _is_individual(session, profile.org_id) else "business"
    )
    persons = await kyc_checks.persons_for(session, profile.org_id)
    checks = await kyc_checks.latest_checks(session, profile.org_id)
    tier, reasons = kyc_risk.evaluate(settings, profile, persons, checks, account_type=account_type)
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
    if await _is_individual(session, profile.org_id):
        blockers.extend(await individual_kyc.validate_owner(session, profile))
        checks = await kyc_checks.latest_checks(session, profile.org_id)
        for kind in ("sanctions", "ban_list"):
            check = checks.get(kind)
            if check is None or check.result not in ("pass", "warn"):
                blockers.append(f"The {kind.replace('_', ' ')} check failed")
        name_match = checks.get("name_match")
        if name_match is None or name_match.result != "pass":
            blockers.append("Owner names on the application don't match their verified IDs")
        return blockers
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
    *,
    manual_override: bool = False,
) -> None:
    if manual_override:
        await individual_kyc.require_admin(session, operator_id)
    if await _is_individual(session, profile.org_id):
        await individual_kyc.require_admin(session, operator_id)
        missing = await individual_kyc.missing_for_submission(session, profile)
        if missing and not manual_override:
            raise ValidationFailedError(
                "Finish these parts first: " + ", ".join(missing), code="kyc_incomplete"
            )
    await rescreen(session, settings, profile)
    blockers = await approval_blockers(session, profile)
    if blockers and not manual_override:
        await session.commit()  # keep the fresh screening results for the operator
        raise ConflictError(
            "Cannot approve yet: " + "; ".join(blockers), code="kyc_approval_blocked"
        )
    if manual_override and profile.status == "needs_info":
        transition(profile, "in_review")
    transition(profile, "approved")
    profile.decided_at = _now()
    profile.decided_by = operator_id
    profile.decision_reason = note.strip() or None
    profile.next_reverification_at = _now() + timedelta(days=settings.kyc_reverify_days)
    if profile.use_case_pending:
        profile.use_case = profile.use_case_pending
        profile.use_case_pending = None
    _audit_operator(
        session,
        profile,
        "kyc.approved",
        operator_id,
        note=note[:500],
        manual_override=manual_override,
        review_warnings=blockers,
    )


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
    # P44 exposure limits. The ops limits form predates them and sends only the three keys
    # above, so a key the payload leaves out keeps its current value instead of being
    # wiped by an unrelated edit.
    previous = dict(profile.limits or {})
    for key in ("max_concurrent_calls", "daily_spend_micros", "established"):
        if limits and key in limits:
            value = limits[key]
            if value is None:
                continue
            valid = isinstance(value, bool) if key == "established" else (
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
            )
            if not valid:
                raise ValidationFailedError(f"{key} has an invalid value")
            cleaned = cleaned if cleaned is not None else {}
            cleaned[key] = value
        elif key in previous:
            cleaned = cleaned if cleaned is not None else {}
            cleaned[key] = previous[key]
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
