"""Is this number allowed to send at all?

Today an unregistered number is discovered by a **carrier rejection** - Bandwidth `4476`,
Telnyx `40300`. That is the worst place to find out: the violation is already recorded
against a brand that takes weeks to rebuild. This module moves the check in front of the
carrier call (phase-4-plan DR-1).

**The one judgement call in here.** A number we have no registration data for is *not*
refused. It is very often legitimately registered directly at the carrier - Bandwidth's
trial account ships exactly such a number - and blocking it would break a working
deployment on the strength of an assumption. So:

    known-bad  -> refuse        (linked to a campaign/TFV that is not approved)
    known-good -> allow         (approved)
    unknown    -> allow, warn   (nothing registered through us; the carrier still gates it)

`REQUIRE_NUMBER_REGISTRATION=true` turns `unknown` into a refusal, for deployments that do
manage every registration here. Note the direction: that flag can only ever make the system
*stricter*. There is deliberately no flag that loosens this, and none that lets a deployment
claim a registration it does not have.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OrgNumber
from app.models.numbers import Campaign, TollFreeVerification

log = structlog.get_logger("compliance.registration")

Verdict = Literal["approved", "pending", "rejected", "unknown"]


@dataclass(frozen=True)
class RegistrationState:
    verdict: Verdict
    detail: str = ""

    @property
    def known_bad(self) -> bool:
        return self.verdict in ("pending", "rejected")


async def registration_state(
    session: AsyncSession, number: OrgNumber
) -> RegistrationState:
    """What we actually know about this number's right to send.

    Toll-free gates on TFV, local gates on 10DLC. They are separate regimes and asking the
    wrong one is how a toll-free number ends up "approved" because somebody's long-code
    campaign was.
    """
    if number.number_type == "tollfree":
        tfv = (
            await session.execute(
                sa.select(TollFreeVerification).where(
                    TollFreeVerification.number_id == number.id
                )
            )
        ).scalar_one_or_none()
        if tfv is None:
            return RegistrationState("unknown", "no toll-free verification on file")
        if tfv.status == "approved":
            return RegistrationState("approved")
        return RegistrationState(
            "rejected" if tfv.status == "rejected" else "pending",
            f"toll-free verification is {tfv.status}",
        )

    if number.campaign_id is None:
        return RegistrationState("unknown", "not linked to a 10DLC campaign")

    campaign = await session.get(Campaign, number.campaign_id)
    if campaign is None:
        return RegistrationState("unknown", "linked campaign no longer exists")
    if campaign.status == "approved":
        return RegistrationState("approved")
    return RegistrationState(
        "rejected" if campaign.status == "rejected" else "pending",
        f"campaign {campaign.name!r} is {campaign.status}",
    )


async def check_number_may_send(
    session: AsyncSession,
    org_id: uuid.UUID,
    number: OrgNumber,
    *,
    require_registration: bool = False,
) -> tuple[bool, str]:
    """(allowed, reason). Reason is operator-facing and says what to DO."""
    if not number.is_active or number.status != "active":
        return False, f"{number.e164} is {number.status} and cannot send"

    state = await registration_state(session, number)
    if state.verdict == "approved":
        return True, ""

    if state.known_bad:
        regime = "toll-free verification" if number.number_type == "tollfree" else "10DLC campaign"
        return False, (
            f"{number.e164} cannot send: {state.detail}. "
            f"Complete its {regime} before sending from this number."
        )

    # unknown
    if require_registration:
        return False, (
            f"{number.e164} has no registration on file and "
            f"REQUIRE_NUMBER_REGISTRATION is on. Register it, or link it to an approved "
            f"campaign."
        )
    log.warning(
        "number_registration_unknown",
        number=number.e164,
        detail=state.detail,
        note="allowed; the carrier remains the enforcing party",
    )
    return True, ""


async def partition_by_eligibility(
    session: AsyncSession,
    numbers: list[OrgNumber],
    *,
    require_registration: bool = False,
) -> tuple[list[OrgNumber], dict[str, str]]:
    """Split a pool into (may send, {e164: why not}).

    Batched into two queries regardless of pool size - this runs on every send, and a
    per-number round trip would put the compliance check on the latency path where it
    would eventually be "optimised" back out.
    """
    if not numbers:
        return [], {}

    local_ids = {n.campaign_id for n in numbers if n.campaign_id is not None}
    tollfree_ids = [n.id for n in numbers if n.number_type == "tollfree"]

    campaigns: dict[uuid.UUID, Campaign] = {}
    if local_ids:
        rows = (
            await session.execute(sa.select(Campaign).where(Campaign.id.in_(local_ids)))
        ).scalars().all()
        campaigns = {c.id: c for c in rows}

    tfvs: dict[uuid.UUID, TollFreeVerification] = {}
    if tollfree_ids:
        rows = (
            await session.execute(
                sa.select(TollFreeVerification).where(
                    TollFreeVerification.number_id.in_(tollfree_ids)
                )
            )
        ).scalars().all()
        tfvs = {t.number_id: t for t in rows}

    allowed: list[OrgNumber] = []
    refused: dict[str, str] = {}
    for number in numbers:
        if not number.is_active or number.status != "active":
            refused[number.e164] = f"{number.e164} is {number.status} and cannot send"
            continue

        if number.number_type == "tollfree":
            tfv = tfvs.get(number.id)
            status = tfv.status if tfv else None
            regime = "toll-free verification"
        else:
            campaign = campaigns.get(number.campaign_id) if number.campaign_id else None
            status = campaign.status if campaign else None
            regime = "10DLC campaign"

        if status == "approved":
            allowed.append(number)
        elif status in ("submitted", "draft", "rejected"):
            refused[number.e164] = (
                f"{number.e164} cannot send: its {regime} is {status}. "
                f"Complete registration before sending from this number."
            )
        elif require_registration:
            refused[number.e164] = (
                f"{number.e164} has no {regime} on file and REQUIRE_NUMBER_REGISTRATION "
                f"is on."
            )
        else:
            allowed.append(number)
    return allowed, refused
