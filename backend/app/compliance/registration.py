"""Is this number allowed to send at all?

Today an unregistered number is discovered by a **carrier rejection** - Bandwidth `4476`,
Telnyx `40300`. That is the worst place to find out: the violation is already recorded
against a brand that takes weeks to rebuild. This module moves the check in front of the
carrier call (phase-4-plan DR-1).

**The one judgement call in here.** A number we have no registration data for is *not*
refused. It is very often legitimately registered directly at the carrier - Bandwidth's
trial account ships exactly such a number - and blocking it would break a working
deployment on the strength of an assumption. So, for numbers we did not provision:

    known-bad  -> refuse        (linked to a campaign/TFV that is not approved)
    known-good -> allow         (approved)
    unknown    -> allow, warn   (nothing registered through us; the carrier still gates it)

**Telnyx numbers are different and fail closed.** We create their campaigns and TFV
submissions ourselves, so "nothing on file" is not "registered directly at the carrier" -
it is a number we failed to finish. For `carrier == "telnyx"`:

    unknown  -> refuse, always (even when REQUIRE_NUMBER_REGISTRATION is off)
    approved -> allow, but only once the carrier registration is corroborated:
                * local: the campaign carries a `carrier_refs['telnyx']` id AND the
                  number's `provisioning['telnyx_campaign_assignment']` marker is
                  `assigned` and matches that campaign/carrier id;
                * toll-free: the verification carries a `carrier_refs['telnyx']` id.

`REQUIRE_NUMBER_REGISTRATION=true` turns `unknown` into a refusal for every carrier, for
deployments that do manage every registration here. Note the direction: that flag can only
ever make the system *stricter*. There is deliberately no flag that loosens this, and none
that lets a deployment claim a registration it does not have. Nothing on the send path
calls the carrier: the Telnyx corroboration reads only what we have already persisted.
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

#: ``OrgNumber.carrier`` value for numbers we buy through and provision with Telnyx. Their
#: campaign/TFV registrations are created by *us*, so an unknown registration is a gap we
#: must not send through - not a number registered directly at the carrier.
TELNYX_CARRIER = "telnyx"


@dataclass(frozen=True)
class RegistrationState:
    verdict: Verdict
    detail: str = ""
    #: True when this number's registration can only come from US: an ``unknown`` verdict is
    #: then a hard refusal rather than the default allow-with-warning. Set for Telnyx
    #: numbers, whose campaigns/TFV submissions this system creates.
    must_register_here: bool = False

    @property
    def known_bad(self) -> bool:
        return self.verdict in ("pending", "rejected")


def _is_telnyx(number: OrgNumber) -> bool:
    return (number.carrier or "").strip().lower() == TELNYX_CARRIER


def _telnyx_carrier_ref(registration: Campaign | TollFreeVerification) -> str | None:
    """The carrier-side id Telnyx gave us for this campaign/TFV, if any.

    ``carrier_refs['telnyx']`` is a plain string in the common case, but the column is
    PortableJSON and brands already store small maps there, so a map is tolerated too.
    """
    refs = getattr(registration, "carrier_refs", None) or {}
    ref = refs.get(TELNYX_CARRIER) if isinstance(refs, dict) else None
    if isinstance(ref, str):
        return ref.strip() or None
    if isinstance(ref, dict):
        for key in ("campaign_id", "id", "ref"):
            value = ref.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _telnyx_local_assignment_ok(
    number: OrgNumber, campaign: Campaign
) -> tuple[bool, str]:
    """Whether this Telnyx local number is really assigned to its (locally approved) campaign.

    Being approved in our own tables is not enough for a number we provision at Telnyx: the
    carrier has to know. That is recorded by the assignment service (still under
    development) in ``OrgNumber.provisioning['telnyx_campaign_assignment']`` as::

        {"state": "assigned", "campaign_id": "<our campaign id>",
         "carrier_id": "<the campaign's carrier_refs['telnyx']>"}

    Anything less - no marker, a different state, ids that belong to a different campaign -
    refuses. Nothing here calls the carrier; it only reads what we have already persisted.
    """
    carrier_ref = _telnyx_carrier_ref(campaign)
    if not carrier_ref:
        return False, "campaign is approved locally but not registered with Telnyx"

    marker = (number.provisioning or {}).get("telnyx_campaign_assignment")
    if not isinstance(marker, dict):
        return False, "number has no Telnyx campaign assignment on file"
    if marker.get("state") != "assigned":
        return False, "number's Telnyx campaign assignment is not complete"
    if str(marker.get("campaign_id") or "") != str(campaign.id):
        return False, "number's Telnyx campaign assignment does not match this campaign"
    marker_carrier = marker.get("carrier_id") or marker.get("carrier_campaign_id")
    if str(marker_carrier or "") != carrier_ref:
        return False, (
            "number's Telnyx campaign assignment does not match the carrier campaign"
        )
    return True, ""


def _tollfree_registration_state(
    number: OrgNumber, tfv: TollFreeVerification | None, *, telnyx: bool
) -> RegistrationState:
    if tfv is None:
        return RegistrationState(
            "unknown",
            "no toll-free verification on file",
            must_register_here=telnyx,
        )
    if tfv.status != "approved":
        return RegistrationState(
            "rejected" if tfv.status == "rejected" else "pending",
            f"toll-free verification is {tfv.status}",
        )
    if telnyx and not _telnyx_carrier_ref(tfv):
        return RegistrationState(
            "pending",
            "toll-free verification is approved locally but not registered with Telnyx",
        )
    return RegistrationState("approved")


def _local_registration_state(
    number: OrgNumber, campaign: Campaign | None, *, telnyx: bool
) -> RegistrationState:
    if number.campaign_id is None:
        return RegistrationState(
            "unknown",
            "not linked to a 10DLC campaign",
            must_register_here=telnyx,
        )
    if campaign is None:
        return RegistrationState(
            "unknown",
            "linked campaign no longer exists",
            must_register_here=telnyx,
        )
    if campaign.status != "approved":
        return RegistrationState(
            "rejected" if campaign.status == "rejected" else "pending",
            f"campaign {campaign.name!r} is {campaign.status}",
        )
    if telnyx:
        ok, detail = _telnyx_local_assignment_ok(number, campaign)
        if not ok:
            return RegistrationState("pending", detail)
    return RegistrationState("approved")


def _registration_state_for(
    number: OrgNumber,
    *,
    campaign: Campaign | None,
    tfv: TollFreeVerification | None,
) -> RegistrationState:
    """Decide from already-loaded rows.

    Shared by the single-number and batch entry points so the two can never drift; the
    batch path feeds it rows it already fetched, so the Telnyx checks add no queries.
    """
    telnyx = _is_telnyx(number)
    if number.number_type == "tollfree":
        return _tollfree_registration_state(number, tfv, telnyx=telnyx)
    return _local_registration_state(number, campaign, telnyx=telnyx)


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
        return _registration_state_for(number, campaign=None, tfv=tfv)

    campaign = None
    if number.campaign_id is not None:
        campaign = await session.get(Campaign, number.campaign_id)
    return _registration_state_for(number, campaign=campaign, tfv=None)


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
    if state.must_register_here:
        return False, (
            f"{number.e164} cannot send: {state.detail}. "
            f"Register it here before sending from this number."
        )
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
    would eventually be "optimised" back out. The Telnyx checks read only the campaign/TFV
    rows fetched here plus the number's own ``provisioning`` column, so they add no query.
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
            campaign = None
            status = tfv.status if tfv else None
            regime = "toll-free verification"
        else:
            tfv = None
            campaign = campaigns.get(number.campaign_id) if number.campaign_id else None
            status = campaign.status if campaign else None
            regime = "10DLC campaign"

        state = _registration_state_for(number, campaign=campaign, tfv=tfv)

        if state.verdict == "approved":
            allowed.append(number)
        elif state.known_bad:
            reason = (
                state.detail
                if _is_telnyx(number)
                else f"its {regime} is {status}"
            )
            refused[number.e164] = (
                f"{number.e164} cannot send: {reason}. "
                f"Complete registration before sending from this number."
            )
        elif state.must_register_here:
            refused[number.e164] = (
                f"{number.e164} cannot send: {state.detail}. "
                f"Register it here before sending from this number."
            )
        elif require_registration:
            refused[number.e164] = (
                f"{number.e164} has no {regime} on file and REQUIRE_NUMBER_REGISTRATION "
                f"is on."
            )
        else:
            allowed.append(number)
    return allowed, refused
