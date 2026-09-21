"""10DLC brands and campaigns, and toll-free verification.

`compliance:manage` throughout. Registration decides what an org is legally permitted to
send, so it sits with compliance rather than with numbers - an agent who can order a number
should not be able to declare a use case on the company's behalf.

Two kinds of "submit" exist here and they are NOT the same thing:

* the legacy ``/submit`` routes only advance the LOCAL record - they never contact a
  carrier and spend nothing;
* the ``/file-telnyx`` routes make a BILLABLE, non-refundable carrier submission, so they
  require a platform operator (super admin) on top of ``compliance:manage`` and an explicit
  ``confirm_non_refundable=true`` acknowledgement.

The three operator ``/status`` routes record a registrar decision. An ``approved``
decision is FAIL-CLOSED: it is only accepted when the local record already carries the
matching Telnyx reference AND a fresh Telnyx GET both confirms the carrier reports it
approved and returns the same identifier. A decision on its own must never make an unfiled
campaign or toll-free verification sendable. Any missing reference, missing key, carrier
error or unconfirmed/unknown status refuses the approval.

A decision that does not actually move the record (already in that status, a stale
``submitted`` after ``approved``, or a terminal state) is not reported as applied: the
transaction is rolled back and a conflict is raised instead of a misleading 200.

A toll-free ``/file-telnyx`` call is different again: the service durably writes a
"pending attempt" marker to ``carrier_refs`` BEFORE the carrier POST, so a second click or
a retry after an ambiguous create timeout is refused rather than filing twice. That timeout
may leave no Telnyx request id to look up at all, so the ``/status`` route CANNOT reconcile
it. The first safe path for that case is the explicit, READ-ONLY operator reconcile route
``POST /tollfree/{tfv_id}/reconcile-telnyx``: it makes one read-only carrier lookup (the
filtered verification list), adopts a request id ONLY on exactly one unambiguous,
exact-match record, never retries the filing and never approves locally. When the carrier
returns zero, multiple or mismatched records that route refuses too and leaves the pending
attempt untouched, so those cases still require an EXPLICIT external reconciliation - a
documented runbook or a Telnyx support investigation - before an operator deliberately
repairs local state.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

import httpx
import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError

from app.auth.deps import OrgContext, require_permission, require_platform_operator
from app.errors import (
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from app.models import OrgNumber
from app.models.numbers import Brand, Campaign, TollFreeVerification
from app.providers.telnyx.registration import TelnyxRegistrationClient
from app.providers.telnyx.registration_status import (
    APPROVED,
    map_brand_status,
    map_campaign_status,
)
from app.providers.telnyx.tollfree_verification import TelnyxTollfreeVerificationClient
from app.services import (
    provider_accounts,
    telnyx_brand_filing,
    telnyx_campaign_filing,
    telnyx_tollfree_filing,
)
from app.services import registration as reg

router = APIRouter(prefix="/api/v1/registration", tags=["registration"])


# ----------------------------------------------------------------------------------
# Carrier-confirmed approvals
# ----------------------------------------------------------------------------------
# An operator ``/status`` decision of ``approved`` is only a registrar's *claim*. Before we
# persist it we require the carrier to actually say so AND to return the record we think we
# are approving, because an ``approved`` campaign or toll-free verification is what
# releases numbers to send. Everything here fails closed: a missing reference, a missing
# key, a carrier error, a mismatched identifier or any unconfirmed status refuses the
# approval rather than trusting the caller.

#: carrier_refs key under which a Telnyx identifier (brandId / campaignId / verification
#: id) is recorded. Matches ``telnyx_brand_filing``.
_TELNYX_REF_KEY = "telnyx"

#: ``app.state`` attribute a test may set to an ``httpx.AsyncClient`` (typically backed by
#: an ``httpx.MockTransport``). When set, these guards reuse it and never open a live
#: socket; when unset the Telnyx client owns and closes its own transport.
_HTTP_CLIENT_STATE_ATTR = "telnyx_http_client"

#: The only ``verificationStatus`` the Telnyx toll-free API documents as approved. Any
#: other value is treated as "not approved" and refuses the decision.
_TFV_APPROVED_STATUS = "verified"

#: Non-PII message used whenever a decision is a no-op (already current, stale, or
#: terminal). Kept generic so no record identifier or caller input is leaked.
_NO_CHANGE_MESSAGE = (
    "The status decision was ignored because it would not change the record: the "
    "requested status is already current, is stale relative to a terminal status, or the "
    "record is terminal."
)


def _secret(value: Any) -> str:
    """Read a SecretStr (or plain) value without ever str()-ing a masked one."""
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return getter()
    return "" if value is None else str(value)


def _is_approval(status: str) -> bool:
    """True when the caller is trying to enter an ``approved`` decision.

    Case/whitespace-insensitive so a variant spelling cannot slip past the guard.
    """
    return status.strip().lower() == APPROVED


def _injected_http_client(request: Request) -> httpx.AsyncClient | None:
    """A transport a test may inject via ``app.state`` so no live carrier call is made."""
    return getattr(request.app.state, _HTTP_CLIENT_STATE_ATTR, None)


def _carrier_ref(record: Any, key: str = _TELNYX_REF_KEY) -> str | None:
    """The stored Telnyx identifier for a local record, or ``None`` when there is none."""
    refs = getattr(record, "carrier_refs", None)
    if not isinstance(refs, dict):
        return None
    value = refs.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _payload_carrier_id(payload: Any, *keys: str) -> str | None:
    """The first populated identifier field of a carrier GET payload, as a string."""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return None


def _carrier_id_matches(payload: Any, expected: str, *keys: str) -> bool:
    """True only when the carrier's returned identifier equals the stored reference, so a
    wrong record (or a mock for a different id) cannot confirm this one."""
    found = _payload_carrier_id(payload, *keys)
    return found is not None and found == expected


async def _resolve_telnyx_settings(session: Any, settings: Any) -> Any:
    """The org's ACTIVE Telnyx account laid over the base settings when there is one,
    else the base settings (a platform-operator run against a globally configured key)."""
    account = await provider_accounts.active_account_for(session, "telnyx")
    if account is None:
        return settings
    return provider_accounts.settings_like_for(settings, account)


def _telnyx_api_key(resolved: Any) -> str:
    """A usable Telnyx key, or refuse - we cannot confirm anything without one."""
    key = _secret(getattr(resolved, "telnyx_api_key", None)).strip()
    if not key:
        raise ValidationFailedError(
            "No Telnyx API key is available; add and verify an active Telnyx account "
            "before recording a carrier-approved decision"
        )
    return key


async def _confirmed_registration_get(
    session: Any,
    settings: Any,
    request: Request,
    carrier_id: str,
    method_name: str,
) -> Any:
    """GET ``carrier_id`` from Telnyx, letting any failure reject the approval.

    Uses the org's active Telnyx account the same way the filing services do. A carrier
    error is never swallowed into a success: it is re-raised as a conflict so the approval
    is refused.
    """
    resolved = await _resolve_telnyx_settings(session, settings)
    api_key = _telnyx_api_key(resolved)
    registration = TelnyxRegistrationClient(
        api_key=api_key, client=_injected_http_client(request)
    )
    try:
        payload = await getattr(registration, method_name)(carrier_id)
    except Exception as exc:  # noqa: BLE001 - any transport/HTTP failure fails closed
        raise ConflictError(
            "Could not confirm the carrier decision with Telnyx; the approval was refused"
        ) from exc
    finally:
        # No-op when the caller injected the client (we do not own it).
        await registration.aclose()
    return payload


async def _require_brand_approved(
    session: Any, settings: Any, request: Request, brand: Brand
) -> None:
    """Refuse an ``approved`` brand decision unless Telnyx confirms it for this brand."""
    carrier_id = _carrier_ref(brand)
    if carrier_id is None:
        raise ConflictError(
            "Cannot record an approved brand decision: no Telnyx brand reference is on "
            "file. File the brand with Telnyx first."
        )
    payload = await _confirmed_registration_get(
        session, settings, request, carrier_id, "get_brand"
    )
    if not _carrier_id_matches(payload, carrier_id, "brandId", "id"):
        raise ConflictError(
            "Telnyx returned a different brand than the one on file; refusing to record "
            "an approved decision against a mismatched record."
        )
    if map_brand_status(payload) != APPROVED:
        raise ConflictError(
            "Telnyx does not currently report this brand as approved; refusing to record "
            "an approved decision the carrier has not confirmed."
        )


async def _require_campaign_approved(
    session: Any, settings: Any, request: Request, campaign: Campaign
) -> None:
    """Refuse an ``approved`` campaign decision unless Telnyx confirms it for this one."""
    carrier_id = _carrier_ref(campaign)
    if carrier_id is None:
        raise ConflictError(
            "Cannot record an approved campaign decision: no Telnyx campaign reference "
            "is on file. File the campaign with Telnyx first."
        )
    payload = await _confirmed_registration_get(
        session, settings, request, carrier_id, "get_campaign"
    )
    if not _carrier_id_matches(payload, carrier_id, "campaignId", "id"):
        raise ConflictError(
            "Telnyx returned a different campaign than the one on file; refusing to "
            "record an approved decision against a mismatched record."
        )
    if map_campaign_status(payload) != APPROVED:
        raise ConflictError(
            "Telnyx does not currently report this campaign as approved; refusing to "
            "record an approved decision the carrier has not confirmed."
        )


def _tfv_is_approved(payload: Any) -> bool:
    """True only for a well-formed payload whose ``verificationStatus`` is ``Verified``.

    ``verificationStatus`` is the field the Telnyx toll-free API documents; there is no
    ``status`` field on this payload, so nothing else is consulted.
    """
    if not isinstance(payload, dict):
        return False
    status = payload.get("verificationStatus")
    return isinstance(status, str) and status.strip().lower() == _TFV_APPROVED_STATUS


async def _require_tfv_approved(
    session: Any, settings: Any, request: Request, tfv: TollFreeVerification
) -> None:
    """Refuse an ``approved`` toll-free decision unless Telnyx confirms it for this one."""
    carrier_id = _carrier_ref(tfv)
    if carrier_id is None:
        raise ConflictError(
            "Cannot record an approved toll-free decision: no Telnyx verification "
            "reference is on file."
        )
    resolved = await _resolve_telnyx_settings(session, settings)
    api_key = _telnyx_api_key(resolved)
    client = TelnyxTollfreeVerificationClient(
        api_key=api_key, client=_injected_http_client(request)
    )
    try:
        payload = await client.get(carrier_id)
    except Exception as exc:  # noqa: BLE001 - any failure fails closed
        raise ConflictError(
            "Could not confirm the toll-free verification with Telnyx; the approval was "
            "refused"
        ) from exc
    finally:
        # No-op when the caller injected the client (we do not own it).
        await client.aclose()
    if not _carrier_id_matches(payload, carrier_id, "id"):
        raise ConflictError(
            "Telnyx returned a different verification than the one on file; refusing to "
            "record an approved decision against a mismatched record."
        )
    if not _tfv_is_approved(payload):
        raise ConflictError(
            "Telnyx does not currently report this toll-free verification as verified; "
            "refusing to record an approved decision the carrier has not confirmed."
        )


# ----------------------------------------------------------------------------------
# Status application
# ----------------------------------------------------------------------------------
async def _apply_status_or_conflict(session: Any, record: Any, payload: StatusIn) -> None:
    """Apply a status decision, but never report a no-op as success.

    ``advance_status`` is monotonic: it ignores a decision that would not move the record
    (already current, stale after a terminal status, or terminal). When it reports no
    change we end the transaction safely and raise, so a caller cannot treat an ignored
    decision as an applied one.
    """
    changed = reg.advance_status(record, payload.status, error=payload.error)
    if not changed:
        await session.rollback()
        raise ConflictError(_NO_CHANGE_MESSAGE)


# ----------------------------------------------------------------------------------
# Brands
# ----------------------------------------------------------------------------------
class BrandIn(BaseModel):
    name: str = Field(min_length=1, max_length=127)
    ein: str | None = Field(default=None, max_length=32)
    entity_type: str = "PRIVATE_PROFIT"
    vertical: str | None = None
    website: str | None = None
    email: str | None = None
    phone: str | None = None
    street: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str = "US"


class BrandOut(BaseModel):
    id: uuid.UUID
    name: str
    entity_type: str
    status: str
    carrier_refs: dict
    last_error: str | None
    #: What still has to be filled in before this can be submitted. Surfaced on READ so the
    #: console can show the gap without the user having to fail a submission to discover it.
    missing_for_submission: list[str]


def _brand_out(b: Brand) -> BrandOut:
    missing = [f for f in reg.REQUIRED_BRAND_FIELDS if not getattr(b, f, None)]
    if b.entity_type != "SOLE_PROPRIETOR" and not b.ein:
        missing.append("ein")
    return BrandOut(
        id=b.id,
        name=b.name,
        entity_type=b.entity_type,
        status=b.status,
        carrier_refs=b.carrier_refs or {},
        last_error=b.last_error,
        missing_for_submission=missing,
    )


@router.get("/brands", response_model=list[BrandOut])
async def list_brands(
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:read"))],
) -> list[BrandOut]:
    rows = (await ctx.session.execute(sa.select(Brand).order_by(Brand.name))).scalars().all()
    return [_brand_out(b) for b in rows]


@router.post("/brands", response_model=BrandOut, status_code=201)
async def create_brand(
    payload: BrandIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> BrandOut:
    brand = Brand(id=uuid.uuid4(), org_id=ctx.org.id, **payload.model_dump())
    ctx.session.add(brand)
    try:
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(f"A brand named {payload.name!r} already exists") from exc
    return _brand_out(brand)


@router.post("/brands/{brand_id}/submit", response_model=BrandOut)
async def submit_brand(
    brand_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> BrandOut:
    """Advance the LOCAL brand record to ``submitted`` - no carrier is contacted.

    This is the legacy local-only path and is kept for compatibility: it spends nothing
    and never reaches Telnyx. The billable carrier submission lives in
    ``/brands/{brand_id}/file-telnyx``.
    """
    brand = await reg.submit_brand(ctx.session, brand_id)
    await ctx.session.commit()
    return _brand_out(brand)


class FileBrandTelnyxIn(BaseModel):
    """Operator body for a Telnyx brand filing.

    ``confirm_non_refundable`` must be the literal ``true``: a 10DLC brand submission is
    billed by the carrier and cannot be refunded, so the caller has to say so explicitly
    rather than opt in by omission.
    """

    confirm_non_refundable: Literal[True]
    company_name: str = Field(min_length=1, max_length=255)
    first_name: str = Field(min_length=1, max_length=255)
    last_name: str = Field(min_length=1, max_length=255)
    brand_relationship: str = Field(min_length=1, max_length=255)


@router.post("/brands/{brand_id}/file-telnyx", response_model=BrandOut)
async def file_brand_telnyx(
    brand_id: uuid.UUID,
    payload: FileBrandTelnyxIn,
    request: Request,
    _ops: Annotated[None, Depends(require_platform_operator)],
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> BrandOut:
    """File this brand with Telnyx - a BILLABLE, non-refundable carrier call.

    Unlike ``/submit`` (which only moves the LOCAL record and never contacts a carrier),
    this reaches Telnyx and creates a real 10DLC brand that cannot be refunded. It is
    restricted to a platform operator (super admin) that also holds ``compliance:manage``,
    and requires an explicit ``confirm_non_refundable=true``. The carrier is only ever
    called by ``file_brand_with_telnyx`` using the org's configured Telnyx credentials;
    no secret is read or echoed here.
    """
    brand = await ctx.session.get(Brand, brand_id)
    if brand is None:
        raise NotFoundError("Brand not found")
    settings = request.app.state.settings
    brand = await telnyx_brand_filing.file_brand_with_telnyx(
        ctx.session,
        settings,
        brand,
        company_name=payload.company_name,
        first_name=payload.first_name,
        last_name=payload.last_name,
        brand_relationship=payload.brand_relationship,
    )
    return _brand_out(brand)


# ----------------------------------------------------------------------------------
# Campaigns
# ----------------------------------------------------------------------------------
class CampaignIn(BaseModel):
    brand_id: uuid.UUID
    name: str = Field(min_length=1, max_length=127)
    use_case: str = "MIXED"
    description: str | None = None
    opt_in_process: str | None = None
    sample_messages: list[str] = []
    help_message: str | None = None
    opt_out_message: str | None = None


class CampaignOut(BaseModel):
    id: uuid.UUID
    brand_id: uuid.UUID
    name: str
    use_case: str
    status: str
    carrier_refs: dict
    last_error: str | None
    number_count: int
    missing_for_submission: list[str]


async def _campaign_out(session, c: Campaign) -> CampaignOut:
    missing = [f for f in reg.REQUIRED_CAMPAIGN_FIELDS if not getattr(c, f, None)]
    if not (c.sample_messages or []):
        missing.append("sample_messages")
    if not c.opt_out_message:
        missing.append("opt_out_message")
    return CampaignOut(
        id=c.id,
        brand_id=c.brand_id,
        name=c.name,
        use_case=c.use_case,
        status=c.status,
        carrier_refs=c.carrier_refs or {},
        last_error=c.last_error,
        number_count=await reg.numbers_on_campaign(session, c.id),
        missing_for_submission=missing,
    )


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:read"))],
) -> list[CampaignOut]:
    rows = (
        await ctx.session.execute(sa.select(Campaign).order_by(Campaign.name))
    ).scalars().all()
    return [await _campaign_out(ctx.session, c) for c in rows]


@router.post("/campaigns", response_model=CampaignOut, status_code=201)
async def create_campaign(
    payload: CampaignIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> CampaignOut:
    brand = await ctx.session.get(Brand, payload.brand_id)
    if brand is None:
        raise NotFoundError("Brand not found")
    campaign = Campaign(id=uuid.uuid4(), org_id=ctx.org.id, **payload.model_dump())
    ctx.session.add(campaign)
    try:
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError(f"A campaign named {payload.name!r} already exists") from exc
    return await _campaign_out(ctx.session, campaign)


@router.post("/campaigns/{campaign_id}/submit", response_model=CampaignOut)
async def submit_campaign(
    campaign_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> CampaignOut:
    """Advance the LOCAL campaign record to ``submitted`` - no carrier is contacted.

    Legacy local-only path, kept for compatibility: it spends nothing. Use
    ``/campaigns/{campaign_id}/file-telnyx`` for the billable Telnyx submission.
    """
    campaign = await reg.submit_campaign(ctx.session, campaign_id)
    await ctx.session.commit()
    return await _campaign_out(ctx.session, campaign)


class FileCampaignTelnyxIn(BaseModel):
    """Operator body for a Telnyx campaign filing.

    ``assertions`` is the explicit consent/financial attestation map passed straight through
    to the carrier payload builder. ``confirm_non_refundable`` must be the literal ``true``:
    a 10DLC campaign submission is billed by the carrier and cannot be refunded.
    """

    confirm_non_refundable: Literal[True]
    assertions: dict[str, bool]


@router.post("/campaigns/{campaign_id}/file-telnyx", response_model=CampaignOut)
async def file_campaign_telnyx(
    campaign_id: uuid.UUID,
    payload: FileCampaignTelnyxIn,
    request: Request,
    _ops: Annotated[None, Depends(require_platform_operator)],
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> CampaignOut:
    """File this campaign with Telnyx - a BILLABLE, non-refundable carrier call.

    Unlike ``/submit`` (which only moves the LOCAL record and never contacts a carrier),
    this reaches Telnyx and creates a real 10DLC campaign that cannot be refunded, after
    the service verifies the parent brand is approved both locally and at Telnyx. It is
    restricted to a platform operator (super admin) that also holds ``compliance:manage``,
    and requires an explicit ``confirm_non_refundable=true``. The carrier is only ever
    called by ``file_campaign_with_telnyx`` using the org's configured Telnyx credentials;
    no secret is read or echoed here.
    """
    campaign = await ctx.session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError("Campaign not found")
    settings = request.app.state.settings
    campaign = await telnyx_campaign_filing.file_campaign_with_telnyx(
        ctx.session,
        settings,
        campaign,
        assertions=payload.assertions,
    )
    return await _campaign_out(ctx.session, campaign)


class StatusIn(BaseModel):
    status: str
    error: str | None = None


@router.post("/campaigns/{campaign_id}/status", response_model=CampaignOut)
async def set_campaign_status(
    campaign_id: uuid.UUID,
    payload: StatusIn,
    request: Request,
    _ops: Annotated[None, Depends(require_platform_operator)],
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> CampaignOut:
    """Record a registrar decision.

    Monotonic: a stale `submitted` arriving after `approved` is ignored, not applied.
    Carriers retry unordered, and demoting an approved campaign would stop every number on
    it from sending until somebody noticed.

    An ``approved`` decision is fail-closed: it is only accepted when the campaign already
    carries a Telnyx campaign reference, a fresh Telnyx GET returns that same campaign and
    maps to approved. A decision alone must never make an unfiled campaign sendable. Any
    missing reference, missing key, carrier error, mismatched identifier or unconfirmed
    status refuses the approval; non-approved decisions keep the existing monotonic
    behaviour. A decision that changes nothing (already current, stale, or terminal) is not
    reported as applied: the transaction is rolled back and a conflict is raised.
    """
    campaign = await ctx.session.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError("Campaign not found")
    if _is_approval(payload.status):
        await _require_campaign_approved(
            ctx.session, request.app.state.settings, request, campaign
        )
    await _apply_status_or_conflict(ctx.session, campaign, payload)
    await ctx.session.commit()
    return await _campaign_out(ctx.session, campaign)


@router.post("/brands/{brand_id}/status", response_model=BrandOut)
async def set_brand_status(
    brand_id: uuid.UUID,
    payload: StatusIn,
    request: Request,
    _ops: Annotated[None, Depends(require_platform_operator)],
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> BrandOut:
    """Record a registrar decision for a brand.

    An ``approved`` decision is fail-closed: it is only accepted when the brand already
    carries a Telnyx brand reference, a fresh Telnyx GET returns that same brand and maps
    to approved. Any missing reference, missing key, carrier error, mismatched identifier
    or unknown status refuses the approval; non-approved decisions keep the existing
    monotonic behaviour. A decision that changes nothing (already current, stale, or
    terminal) is not reported as applied: the transaction is rolled back and a conflict is
    raised.
    """
    brand = await ctx.session.get(Brand, brand_id)
    if brand is None:
        raise NotFoundError("Brand not found")
    if _is_approval(payload.status):
        await _require_brand_approved(
            ctx.session, request.app.state.settings, request, brand
        )
    await _apply_status_or_conflict(ctx.session, brand, payload)
    await ctx.session.commit()
    return _brand_out(brand)


# ----------------------------------------------------------------------------------
# Toll-free verification
# ----------------------------------------------------------------------------------
class TfvIn(BaseModel):
    number_id: uuid.UUID
    business_name: str = Field(min_length=1, max_length=255)
    use_case: str = "MIXED"
    use_case_summary: str | None = None
    opt_in_process: str | None = None
    opt_in_screenshot_url: str | None = None
    message_volume: int | None = None
    contact_email: str | None = None


class TfvOut(BaseModel):
    id: uuid.UUID
    number_id: uuid.UUID
    business_name: str
    status: str
    carrier_refs: dict
    last_error: str | None


def _tfv_out(t: TollFreeVerification) -> TfvOut:
    return TfvOut(
        id=t.id,
        number_id=t.number_id,
        business_name=t.business_name,
        status=t.status,
        carrier_refs=t.carrier_refs or {},
        last_error=t.last_error,
    )


@router.get("/tollfree", response_model=list[TfvOut])
async def list_tfv(
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:read"))],
) -> list[TfvOut]:
    rows = (await ctx.session.execute(sa.select(TollFreeVerification))).scalars().all()
    return [_tfv_out(t) for t in rows]


@router.post("/tollfree", response_model=TfvOut, status_code=201)
async def create_tfv(
    payload: TfvIn,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> TfvOut:
    number = await ctx.session.get(OrgNumber, payload.number_id)
    if number is None:
        raise NotFoundError("Number not found")
    if number.number_type != "tollfree":
        # Verifying a long code would produce an "approved" that means nothing, on a number
        # the 10DLC gate is separately refusing.
        raise ValidationFailedError(
            f"{number.e164} is a {number.number_type} number; toll-free verification "
            f"applies only to toll-free numbers. Register it under a 10DLC campaign instead."
        )
    tfv = TollFreeVerification(id=uuid.uuid4(), org_id=ctx.org.id, **payload.model_dump())
    ctx.session.add(tfv)
    try:
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        raise ConflictError("This number already has a verification on file") from exc
    return _tfv_out(tfv)


@router.post("/tollfree/{tfv_id}/submit", response_model=TfvOut)
async def submit_tfv(
    tfv_id: uuid.UUID,
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> TfvOut:
    tfv = await reg.submit_tollfree(ctx.session, tfv_id)
    await ctx.session.commit()
    return _tfv_out(tfv)


class FileTfvTelnyxIn(BaseModel):
    """Operator body for a Telnyx toll-free verification (TFV) filing.

    ``confirm_non_refundable`` must be the literal ``true``: a toll-free verification is
    billed by the carrier and cannot be refunded, so the caller has to say so explicitly
    rather than opt in by omission. ``sole_proprietor`` is required because a business
    filing must still carry the registration fields the service and payload builder
    enforce.

    The Telnyx request fields are declared EXPLICITLY in their documented camelCase form
    rather than accepted as a free-form dictionary, so an unexpected key cannot be smuggled
    into the carrier payload. ``extra="forbid"`` makes that a hard contract: a generic
    ``fields={...}`` wrapper - or any other unknown key - is rejected outright instead of
    being silently discarded by pydantic's default ignore behaviour. ``useCase`` and
    ``messageVolume`` are restricted to the carrier's exact documented enumerations, and
    ``optInWorkflowImageURLs`` must be a non-empty list of non-empty strings (the pure
    payload builder remains the final URL validator). ``businessRegistrationNumber``/
    ``businessRegistrationType``/``businessRegistrationCountry`` are optional HERE only
    because a sole proprietor legitimately has none to give; the service still requires them
    for a non-sole-proprietor filing.
    """

    model_config = ConfigDict(extra="forbid")

    confirm_non_refundable: Literal[True]
    sole_proprietor: bool
    businessName: str = Field(min_length=1, max_length=255)
    corporateWebsite: str = Field(min_length=1, max_length=255)
    businessAddr1: str = Field(min_length=1, max_length=255)
    businessCity: str = Field(min_length=1, max_length=255)
    businessState: str = Field(min_length=1, max_length=255)
    businessZip: str = Field(min_length=1, max_length=32)
    businessContactFirstName: str = Field(min_length=1, max_length=255)
    businessContactLastName: str = Field(min_length=1, max_length=255)
    businessContactEmail: str = Field(min_length=1, max_length=255)
    businessContactPhone: str = Field(min_length=1, max_length=64)
    useCaseSummary: str = Field(min_length=1)
    productionMessageContent: str = Field(min_length=1)
    optInWorkflow: str = Field(min_length=1)
    additionalInformation: str = Field(min_length=1)
    useCase: Literal[
        "Mixed",
        "2FA",
        "General Marketing",
        "Appointments",
        "Conversational / Alerts",
    ]
    messageVolume: Literal[
        "10",
        "100",
        "1,000",
        "10,000",
        "100,000",
        "250,000",
        "500,000",
        "750,000",
        "1,000,000",
        "5,000,000",
        "10,000,000+",
    ]
    optInWorkflowImageURLs: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1)
    businessRegistrationNumber: str | None = None
    businessRegistrationType: str | None = None
    businessRegistrationCountry: str | None = None


@router.post("/tollfree/{tfv_id}/file-telnyx", response_model=TfvOut)
async def file_tfv_telnyx(
    tfv_id: uuid.UUID,
    payload: FileTfvTelnyxIn,
    request: Request,
    _ops: Annotated[None, Depends(require_platform_operator)],
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> TfvOut:
    """File this toll-free verification with Telnyx - a BILLABLE, non-refundable call.

    Unlike ``/submit`` (which only moves the LOCAL record and never contacts a carrier),
    this reaches Telnyx and creates a real toll-free verification request that cannot be
    refunded. It is restricted to a platform operator (super admin) that also holds
    ``compliance:manage``, and requires an explicit ``confirm_non_refundable=true``. The
    carrier is only ever called by ``file_tollfree_verification_with_telnyx`` using the
    org's configured Telnyx credentials; no secret is read or echoed here. The route never
    sets ``approved`` or ``rejected`` locally: filing only ever advances the record to
    ``submitted`` and the carrier decides approval through the separate carrier-confirmed
    path.

    The service writes a durable "pending attempt" marker to ``carrier_refs`` BEFORE the
    carrier POST, so a second click or a retry is refused instead of filing twice. An
    ambiguous create timeout may leave no Telnyx request id to look up at all, so this
    cannot be reconciled through ``/status``; use the explicit, READ-ONLY
    ``POST /tollfree/{tfv_id}/reconcile-telnyx`` route, which performs one exact-match
    carrier lookup and never retries this POST. When the carrier returns zero, multiple or
    mismatched records that route refuses too, and the case still requires an EXPLICIT
    external reconciliation - a documented runbook or a Telnyx support investigation -
    before an operator deliberately repairs local state.
    """
    tfv = await ctx.session.get(TollFreeVerification, tfv_id)
    if tfv is None:
        raise NotFoundError("Toll-free verification not found")
    settings = request.app.state.settings
    fields = payload.model_dump(exclude={"confirm_non_refundable", "sole_proprietor"})
    tfv = await telnyx_tollfree_filing.file_tollfree_verification_with_telnyx(
        ctx.session,
        settings,
        tfv,
        fields=fields,
        sole_proprietor=payload.sole_proprietor,
        client=_injected_http_client(request),
    )
    return _tfv_out(tfv)


@router.post("/tollfree/{tfv_id}/reconcile-telnyx", response_model=TfvOut)
async def reconcile_tfv_telnyx(
    tfv_id: uuid.UUID,
    request: Request,
    _ops: Annotated[None, Depends(require_platform_operator)],
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> TfvOut:
    """Explicitly reconcile an ambiguous, timed-out Telnyx toll-free filing.

    The ``/file-telnyx`` service wrote a durable "pending attempt" marker before its
    carrier POST, so an ambiguous create timeout leaves the record "attempted, outcome
    unknown" with no request id the ``/status`` route can look up. This is the FIRST safe
    path for that case: an explicit operator repair that performs exactly ONE read-only
    carrier lookup (the filtered verification list) and adopts a request id ONLY when that
    result is unambiguous - exactly one record matching this exact business name and
    number. It NEVER retries the filing, never POSTs, never creates or spends, and never
    approves locally; zero, multiple, paginated or mismatched results are refused and still
    require a documented runbook or a Telnyx support investigation.

    This route is restricted to a platform operator (super admin) that also holds
    ``compliance:manage``. Because it can never create or spend, it needs no
    ``confirm_non_refundable`` acknowledgement. It clears no markers, mutates no refs or
    status and implements no matching itself: every safeguard belongs to
    ``reconcile_tollfree_filing_with_telnyx``, which owns the carrier call using the org's
    configured Telnyx credentials; no secret is read or echoed here.
    """
    tfv = await ctx.session.get(TollFreeVerification, tfv_id)
    if tfv is None:
        raise NotFoundError("Toll-free verification not found")
    settings = request.app.state.settings
    tfv = await telnyx_tollfree_filing.reconcile_tollfree_filing_with_telnyx(
        ctx.session,
        settings,
        tfv,
        client=_injected_http_client(request),
    )
    return _tfv_out(tfv)


@router.post("/tollfree/{tfv_id}/status", response_model=TfvOut)
async def set_tfv_status(
    tfv_id: uuid.UUID,
    payload: StatusIn,
    request: Request,
    _ops: Annotated[None, Depends(require_platform_operator)],
    ctx: Annotated[OrgContext, Depends(require_permission("compliance:manage"))],
) -> TfvOut:
    """Record a registrar decision for a toll-free verification.

    An ``approved`` decision is fail-closed: it is only accepted when the verification
    already carries a Telnyx reference, a fresh Telnyx GET returns that same request and
    reports the documented ``verificationStatus`` of ``Verified``. An unfiled verification
    must never become sendable on a decision alone. Any missing reference, missing key,
    carrier error, mismatched identifier or unknown status refuses the approval;
    non-approved decisions keep the existing monotonic behaviour. A decision that changes
    nothing (already current, stale, or terminal) is not reported as applied: the
    transaction is rolled back and a conflict is raised.

    This route records a registrar decision only. It does NOT reconcile a pending Telnyx
    filing attempt: an ambiguous create timeout may leave no request id to look up, so the
    pending marker written by ``/file-telnyx`` is not repaired here. Use the explicit,
    READ-ONLY ``POST /tollfree/{tfv_id}/reconcile-telnyx`` route - the first safe
    reconciliation path, which adopts a request id only on a single exact-match carrier
    result - for that; zero, multiple or mismatched results still require an explicit
    external reconciliation (a documented runbook or a Telnyx support investigation), not a
    status change here.
    """
    tfv = await ctx.session.get(TollFreeVerification, tfv_id)
    if tfv is None:
        raise NotFoundError("Toll-free verification not found")
    if _is_approval(payload.status):
        await _require_tfv_approved(
            ctx.session, request.app.state.settings, request, tfv
        )
    await _apply_status_or_conflict(ctx.session, tfv, payload)
    await ctx.session.commit()
    return _tfv_out(tfv)
