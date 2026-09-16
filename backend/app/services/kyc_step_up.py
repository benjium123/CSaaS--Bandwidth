"""P41 selfie step-up: a fresh Stripe Identity check before a risky action.

The action is unlocked only when ALL of these hold:
  - the check is ``verified`` and finished within STEP_UP_SELFIE_MINUTES
  - it was started for THIS action by THIS user
  - the person on the ID is the same person this user was verified as during business
    verification (identity hash of name + date of birth) - so a verified owner cannot hand
    their login to someone else who then passes a selfie check with their own ID
  - it has not already been used (single use)

With KYC_ENFORCED off (development, and every pre-P41 test) step-ups are not required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY
from app.errors import ValidationFailedError
from app.models import STEP_UP_ACTIONS, KycPerson, KycStepUp, User
from app.services import stripe_client


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def verified_identity_hashes(session: AsyncSession, user_id: uuid.UUID) -> set[str]:
    # JUSTIFIED allow_unscoped: a person is the same person in every workspace they were
    # verified for, and a step-up is not tied to one org.
    rows = (
        (
            await session.execute(
                sa.select(KycPerson.identity_hash)
                .where(
                    KycPerson.user_id == user_id,
                    KycPerson.status == "verified",
                    KycPerson.identity_hash.is_not(None),
                )
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    return set(rows)


async def start(
    session: AsyncSession, settings: Settings, user: User, *, action: str, return_url: str
) -> tuple[KycStepUp, str]:
    if action not in STEP_UP_ACTIONS:
        raise ValidationFailedError(f"Unknown action: {action}")
    if not await verified_identity_hashes(session, user.id):
        raise ValidationFailedError(
            "This action needs an ID-verified person. Verify your identity in Business "
            "verification first.",
            code="identity_not_verified",
        )
    row = KycStepUp(id=uuid.uuid4(), user_id=user.id, action=action, status="pending")
    session.add(row)
    await session.flush()
    created = await stripe_client.create_verification_session(
        settings,
        metadata={"purpose": "step_up", "step_up_id": str(row.id), "user_id": str(user.id)},
        return_url=return_url,
    )
    row.stripe_verification_session_id = created["id"]
    return row, created["url"]


async def apply_outcome(session: AsyncSession, vs_id: str, outcome: dict) -> None:
    from app.services.kyc import identity_hash

    row = (
        await session.execute(
            sa.select(KycStepUp).where(KycStepUp.stripe_verification_session_id == vs_id)
        )
    ).scalar_one_or_none()
    if row is None or row.status in ("verified", "failed"):
        return
    status = outcome.get("status")
    if status == "verified":
        row.identity_hash = identity_hash(
            outcome.get("first_name"), outcome.get("last_name"), outcome.get("dob")
        )
        expected = await verified_identity_hashes(session, row.user_id)
        if row.identity_hash and row.identity_hash in expected:
            row.status = "verified"
            row.verified_at = datetime.now(timezone.utc)
        else:
            row.status = "failed"
            row.last_error = "The ID does not belong to the verified person on this account"
    elif status == "processing":
        row.status = "processing"
    elif status == "requires_input":
        row.status = "failed"
        row.last_error = (outcome.get("error_code") or "Verification failed")[:255]
    elif status == "canceled":
        row.status = "canceled"


async def has_fresh_selfie(
    session: AsyncSession,
    settings: Settings,
    user: User,
    *,
    action: str,
    now: datetime | None = None,
    consume: bool = True,
) -> bool:
    if not settings.kyc_enforced:
        return True
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=settings.step_up_selfie_minutes)
    rows = (
        (
            await session.execute(
                sa.select(KycStepUp)
                .where(
                    KycStepUp.user_id == user.id,
                    KycStepUp.action == action,
                    KycStepUp.status == "verified",
                    KycStepUp.consumed_at.is_(None),
                )
                .order_by(KycStepUp.verified_at.desc())
            )
        )
        .scalars()
        .all()
    )
    expected = await verified_identity_hashes(session, user.id)
    for row in rows:
        verified_at = _aware(row.verified_at)
        if verified_at is None or verified_at < cutoff:
            continue
        if row.identity_hash not in expected:
            continue
        if consume:
            row.consumed_at = now
            await session.flush()
        return True
    return False
