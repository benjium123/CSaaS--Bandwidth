"""Individual (person) KYC helpers for the P41 stack.

An individual account is verified as a PERSON: exactly one owner, proven with a
government ID, on a use case that sends no SMS/MMS. These helpers read and validate
only - services/kyc.py owns the writes. The user-submit binding (which signed-in user a
submission is recorded against) stays in the kyc submit integration, so validate_owner
takes an OPTIONAL user_id.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import PermissionDeniedError, ValidationFailedError
from app.models import (
    KycPerson,
    KycProfile,
    Org,
    OrgMembership,
    PlatformOperator,
    Role,
)
from app.services import operators

__all__ = [
    "is_individual",
    "missing_for_submission",
    "require_admin",
    "validate_owner",
    "validate_person_creation",
]

ACCOUNT_TYPE_INDIVIDUAL = "individual"
OWNER_ROLE = "owner"
#: Roles meaning "owns or controls the account". An individual may have one owner only.
OWNER_LIKE_ROLES: tuple[str, ...] = ("owner", "beneficial_owner")
IDENTITY_PROVIDER_DIDIT = "didit"
PERSON_VERIFIED_STATUS = "verified"
OPERATOR_ADMIN_ROLE = "admin"


async def is_individual(session: AsyncSession, org_id: uuid.UUID) -> bool:
    """True when the workspace is an individual account.

    Callers check this BEFORE validate_person_creation: that helper deliberately ignores
    account_type so the internal add_person flow for businesses is unaffected.
    """
    org = await session.get(Org, org_id)
    return org is not None and org.account_type == ACCOUNT_TYPE_INDIVIDUAL


async def validate_owner(
    session: AsyncSession,
    profile: KycProfile,
    user_id: uuid.UUID | None = None,
) -> list[str]:
    """Human-readable reasons the owner is not yet acceptable ([] = fine).

    Checks exactly one owner and no other owner-like row, an owner membership for the
    bound user, an optional match against the submitting user, and a verified Didit
    identity whose name matches the declared legal name. No document or company checks -
    an individual has no registry filing to prove.

    Also blocks a stale annual re-verification: when the profile's
    next_reverification_at is due (<= now UTC), the owner's verified_at must be at or
    after that due date. Initial profiles have next_reverification_at None and are
    untouched.
    """
    problems: list[str] = []
    rows = (
        (await session.execute(sa.select(KycPerson).where(KycPerson.org_id == profile.org_id)))
        .scalars()
        .all()
    )
    owners = [r for r in rows if r.role == OWNER_ROLE]
    others = [r for r in rows if r.role in OWNER_LIKE_ROLES and r.role != OWNER_ROLE]

    if not owners:
        problems.append("No owner has been added to this application.")
    elif len(owners) > 1:
        problems.append(
            "More than one owner is recorded; an individual account must have exactly one."
        )
    if others:
        problems.append(
            "An individual account cannot list beneficial owners or other controlling people."
        )
    if len(owners) != 1:
        return problems

    owner = owners[0]
    if owner.user_id is None:
        problems.append("The owner is not linked to a user account.")
        return problems
    if user_id is not None and owner.user_id != user_id:
        problems.append("The owner on file is not the signed-in user.")
    if not await _has_owner_membership(session, profile.org_id, owner.user_id):
        problems.append("The linked user is not an owner of this workspace.")

    if owner.status != PERSON_VERIFIED_STATUS:
        problems.append("The owner's identity has not been verified yet.")
    if owner.identity_provider != IDENTITY_PROVIDER_DIDIT:
        problems.append("The owner's identity was not verified with Didit.")
    if not owner.provider_session_id:
        problems.append("The owner's identity check has no provider session.")
    if not owner.identity_hash:
        problems.append("The owner's identity record is incomplete.")
    if not owner.verified_name:
        problems.append("The owner's verified name is missing.")
    elif profile.legal_name and not _names_match(profile.legal_name, owner.verified_name):
        problems.append("The owner's verified name does not match the account's legal name.")

    if _reverification_is_stale(profile.next_reverification_at, owner.verified_at):
        problems.append("Repeat the identity check for annual re-verification")

    return problems


async def missing_for_submission(session: AsyncSession, profile: KycProfile) -> list[str]:
    """Bare stable keys for everything still missing ([] = ready to submit).

    Keys are the profile field name, or "use_case.<field>" for the declared use case,
    alongside "agreement" and "id_verification". The frontend matches these exact
    strings; the human-readable detail lives in validate_owner.
    """
    from app.services.kyc import AGREEMENT_VERSION

    missing: list[str] = []

    if _blank(profile.country):
        missing.append("country")
    if _blank(profile.legal_name):
        missing.append("legal_name")
    if _blank(profile.business_email):
        missing.append("business_email")
    if _blank(profile.business_phone):
        missing.append("business_phone")

    use_case = profile.use_case or {}
    simplified = use_case.get("application_version") in (2, 3)
    if use_case.get("application_version") == 3 and _blank(use_case.get("business_description")):
        missing.append("use_case.business_description")
    if _blank(use_case.get("description")):
        missing.append("use_case.description")
    if _blank(use_case.get("vertical")):
        missing.append("use_case.vertical")
    if not simplified and _blank(use_case.get("who_you_contact")):
        missing.append("use_case.who_you_contact")
    if not simplified and _blank(use_case.get("list_source")):
        missing.append("use_case.list_source")
    if _blank(use_case.get("destination_countries")):
        missing.append("use_case.destination_countries")

    calls = use_case.get("monthly_calls")
    if not simplified and not (_is_integer_number(calls) and calls >= 0):
        missing.append("use_case.monthly_calls")

    texts = use_case.get("monthly_texts", 0 if simplified else None)
    if not _is_integer_number(texts) or texts != 0:
        missing.append("use_case.monthly_texts")

    if not profile.agreement_version or profile.agreement_accepted_at is None:
        missing.append("agreement")
    elif profile.agreement_version != AGREEMENT_VERSION:
        missing.append("agreement")

    if await validate_owner(session, profile):
        missing.append("id_verification")

    return missing


async def require_admin(session: AsyncSession, operator_id: uuid.UUID) -> PlatformOperator:
    """Return the active platform operator, or raise if they are not an admin."""
    operator = await operators.get_active(session, operator_id)
    if operator is None or not operators.role_satisfies(operator.role, OPERATOR_ADMIN_ROLE):
        raise PermissionDeniedError(
            "Platform admin access is required.", code="individual_admin_required"
        )
    return operator


async def validate_person_creation(
    session: AsyncSession,
    profile: KycProfile,
    role: str,
    user_id: uuid.UUID | None,
) -> None:
    """Raise ValidationFailedError unless this person may be added to an individual app.

    The org row is locked first so two concurrent requests cannot both see "no owner
    yet" and create two owners (SQLite serialises the write instead). The caller must
    already have confirmed the account is an individual; this does not check account_type.
    """
    if profile.org_id is not None:
        await session.execute(sa.select(Org.id).where(Org.id == profile.org_id).with_for_update())

    if role != OWNER_ROLE:
        raise ValidationFailedError(
            "An individual account may only add a person with the owner role."
        )
    if user_id is None:
        raise ValidationFailedError("The owner must be linked to a user account.")
    if not await _has_owner_membership(session, profile.org_id, user_id):
        raise ValidationFailedError("That user is not an owner of this workspace.")

    existing = await session.execute(
        sa.select(KycPerson.id).where(
            KycPerson.org_id == profile.org_id, KycPerson.role == OWNER_ROLE
        )
    )
    if existing.first() is not None:
        raise ValidationFailedError("This account already has an owner.")


# --- internals -------------------------------------------------------------------------


async def _has_owner_membership(
    session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """True when user_id holds a role named "owner" in this org."""
    row = (
        await session.execute(
            sa.select(OrgMembership.id)
            .join(Role, Role.id == OrgMembership.role_id)
            .where(
                OrgMembership.org_id == org_id,
                OrgMembership.user_id == user_id,
                Role.org_id == org_id,
                Role.name == OWNER_ROLE,
            )
        )
    ).first()
    return row is not None


def _reverification_is_stale(
    next_reverification_at: datetime | None,
    verified_at: datetime | None,
) -> bool:
    """True when an annual re-verification is due and the owner has not re-verified.

    A profile with no next_reverification_at (an initial application) is never stale.
    A due date in the future is not stale either. When the due date has passed, the
    owner's verified_at must be at or after it; a missing verified_at counts as stale.
    Naive datetimes (as SQLite returns) are treated as UTC before comparison.
    """
    if next_reverification_at is None:
        return False
    due = _as_utc(next_reverification_at)
    if due > datetime.now(timezone.utc):
        return False
    if verified_at is None:
        return True
    return _as_utc(verified_at) < due


def _as_utc(value: datetime) -> datetime:
    """Return value as an aware UTC datetime, assuming UTC for naive values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _names_match(a: str | None, b: str | None) -> bool:
    """Conservative full-name match: casefolded, whitespace-collapsed equality only.

    Deliberately NOT the fuzzy names_match() in kyc_checks - a declared legal name and a
    verified name must be the same name, never merely overlapping.
    """
    na, nb = _normalized_name(a), _normalized_name(b)
    return bool(na) and na == nb


def _normalized_name(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _is_integer_number(value: object) -> bool:
    """True only for a genuine whole number.

    bool is rejected (True == 1), strings are rejected, and a float must be integral -
    int(0.9) == 0 must never satisfy a monthly_texts check.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and value.is_integer()


def _blank(value: object) -> bool:
    """True when a field has not really been filled in (0 and False count as filled)."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False
