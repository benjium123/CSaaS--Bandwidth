"""Brand, campaign and toll-free registration state.

The state machine is ours, not the carrier's, for one reason: **a late webhook must never
walk an approved registration backwards**. Carriers retry unordered (ARCHITECTURE D6), so a
`submitted` callback can and does arrive after the `approved` one. If that demoted the
campaign, every number on it would stop sending until somebody noticed.

So transitions are monotonic by rank, exactly like message status - and for the same
reason. This is the third time that shape has been needed (messages, media, registration);
each one is a place where an unordered retry could otherwise undo a fact.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models.numbers import (
    REGISTRATION_RANK,
    TERMINAL_REGISTRATION,
    Brand,
    Campaign,
    TollFreeVerification,
)

log = structlog.get_logger("registration")

Registerable = Brand | Campaign | TollFreeVerification


def advance_status(entity: Registerable, new_status: str, *, error: str | None = None) -> bool:
    """Move a registration forward, never backward. Returns whether anything changed.

    Two guards, and they are not the same guard:
      * rank never decreases - a stale `submitted` cannot undo `approved`
      * a terminal status is never replaced by a DIFFERENT terminal status - an `approved`
        campaign does not become `rejected` because a duplicate callback arrived
    """
    if new_status not in REGISTRATION_RANK:
        raise ValidationFailedError(f"Unknown registration status {new_status!r}")

    # A column default is applied at INSERT, so an entity that has not been flushed yet
    # has status=None. Treat that as "draft" rather than indexing None into the rank map.
    current = entity.status or "draft"
    if current == new_status:
        entity.status = current
        return False

    if current in TERMINAL_REGISTRATION:
        log.warning(
            "registration_terminal_status_ignored",
            entity=type(entity).__name__,
            entity_id=str(entity.id),
            current=current,
            attempted=new_status,
        )
        return False

    if REGISTRATION_RANK[new_status] < REGISTRATION_RANK[current]:
        log.warning(
            "registration_status_regression_ignored",
            entity=type(entity).__name__,
            entity_id=str(entity.id),
            current=current,
            attempted=new_status,
        )
        return False

    entity.status = new_status
    entity.last_error = (error or None) if new_status == "rejected" else None
    log.info(
        "registration_status_advanced",
        entity=type(entity).__name__,
        entity_id=str(entity.id),
        status=new_status,
    )
    return True


REQUIRED_BRAND_FIELDS = ("name", "email", "street", "city", "state", "postal_code")
REQUIRED_CAMPAIGN_FIELDS = ("name", "use_case", "description", "opt_in_process")


def validate_brand_for_submission(brand: Brand) -> None:
    """Refuse locally what the carrier would refuse days later.

    10DLC vetting is slow and a rejection costs real time, so anything we can catch before
    submission we catch before submission.
    """
    missing = [f for f in REQUIRED_BRAND_FIELDS if not getattr(brand, f, None)]
    if missing:
        raise ValidationFailedError(
            f"Brand cannot be submitted; missing: {', '.join(missing)}"
        )
    if brand.entity_type != "SOLE_PROPRIETOR" and not brand.ein:
        raise ValidationFailedError(
            "An EIN is required for every entity type except SOLE_PROPRIETOR"
        )


def validate_campaign_for_submission(campaign: Campaign) -> None:
    missing = [f for f in REQUIRED_CAMPAIGN_FIELDS if not getattr(campaign, f, None)]
    if missing:
        raise ValidationFailedError(
            f"Campaign cannot be submitted; missing: {', '.join(missing)}"
        )
    samples = campaign.sample_messages or []
    if len(samples) < 1:
        raise ValidationFailedError(
            "At least one sample message is required; registrars reject campaigns without one"
        )
    if not campaign.opt_out_message:
        raise ValidationFailedError(
            "An opt-out message is required - it is what STOP replies with"
        )


async def submit_brand(session: AsyncSession, brand_id: uuid.UUID) -> Brand:
    brand = await session.get(Brand, brand_id)
    if brand is None:
        raise NotFoundError("Brand not found")
    if brand.status in TERMINAL_REGISTRATION:
        raise ConflictError(f"Brand is already {brand.status}")
    validate_brand_for_submission(brand)
    advance_status(brand, "submitted")
    return brand


async def submit_campaign(session: AsyncSession, campaign_id: uuid.UUID) -> Campaign:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError("Campaign not found")
    if campaign.status in TERMINAL_REGISTRATION:
        raise ConflictError(f"Campaign is already {campaign.status}")

    brand = await session.get(Brand, campaign.brand_id)
    if brand is None or brand.status != "approved":
        # A campaign cannot outrun its brand: submitting one against an unapproved brand is
        # an automatic registrar rejection, and rejections cost weeks.
        raise ValidationFailedError(
            "The brand must be approved before its campaigns can be submitted"
        )
    validate_campaign_for_submission(campaign)
    advance_status(campaign, "submitted")
    return campaign


async def submit_tollfree(session: AsyncSession, tfv_id: uuid.UUID) -> TollFreeVerification:
    tfv = await session.get(TollFreeVerification, tfv_id)
    if tfv is None:
        raise NotFoundError("Toll-free verification not found")
    if tfv.status in TERMINAL_REGISTRATION:
        raise ConflictError(f"Verification is already {tfv.status}")
    missing = [
        f
        for f in ("business_name", "use_case", "use_case_summary", "opt_in_process")
        if not getattr(tfv, f, None)
    ]
    if missing:
        raise ValidationFailedError(
            f"Verification cannot be submitted; missing: {', '.join(missing)}"
        )
    advance_status(tfv, "submitted")
    return tfv


async def numbers_on_campaign(session: AsyncSession, campaign_id: uuid.UUID) -> int:
    from app.models import OrgNumber

    return int(
        (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(OrgNumber)
                .where(OrgNumber.campaign_id == campaign_id)
            )
        ).scalar_one()
    )
