"""Self-serve 10DLC: the customer pays the carrier fees at cost, then this drives filing.

The carrier writes stay in the existing services (telnyx_brand_filing,
telnyx_campaign_filing, telnyx_number_association), with every guard they already have:
one attempt per record, a durable marker committed before each POST, never an automatic
retry of an ambiguous write. This module only decides WHEN to call them, and turns the
carrier's own verdict into the local status and approval evidence that the send gate reads.
Nothing here marks anything approved that Telnyx has not reported as approved.

Fees are Telnyx's (support.telnyx.com "10DLC Fees and Charges"): brand registration $4.50
and campaign review $15 once, plus the monthly campaign fee, which Telnyx bills three months
upfront. Each is sold at cost plus a markup (``sell_cents``). Checkout takes the one-time
fees plus three months today, and the Stripe subscription charges the monthly fee from month
four. On top, Ringlite charges a one-time SERVICE_FEE_CENTS registration fee. The customer
is shown totals only, never the split.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa
import structlog

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ConflictError, FeatureUnavailableError, ValidationFailedError
from app.models import Brand, Campaign, OrgNumber, TenDlcRegistration
from app.services import audit as audit_svc
from app.services import stripe_client, tendlc_notices

log = structlog.get_logger("tendlc")

BRAND_FEE_CENTS = 450
CAMPAIGN_REVIEW_CENTS = 1500
#: Ringlite's commission per registration, on top of the carrier fees. Not refunded if the
#: carrier refuses the business (the filing work was done).
SERVICE_FEE_CENTS = 500
UPFRONT_MONTHS = 3
MONTHLY_CENTS = {"standard": 1000, "sole_proprietor": 200}
#: Owner 2026-10-17: every carrier fee above (Telnyx's own cost) is sold at cost + 25%, but
#: never less than $1.50 on top - Stripe takes its cut too. Brand $4.50 -> $6.00, monthly
#: $10.00 -> $12.50 and $2.00 -> $3.50. The campaign review is a flat $18 (owner's figure).
MARKUP_BPS = 2_500
MIN_MARKUP_CENTS = 150
CAMPAIGN_REVIEW_SELL_CENTS = 1800


def sell_cents(cost_cents: int) -> int:
    """What the customer pays for a carrier fee that costs us ``cost_cents``."""
    return cost_cents + max(-(-cost_cents * MARKUP_BPS // 10_000), MIN_MARKUP_CENTS)


PRICE_SETTING = {
    "standard": "stripe_tendlc_standard_price_id",
    "sole_proprietor": "stripe_tendlc_sole_prop_price_id",
}
#: How the ISV relates to the brand. Every self-serve customer is a direct, small account.
BRAND_RELATIONSHIP = "BASIC_ACCOUNT"

#: Stages the background job still has work to do on.
WORKING_STAGES = (
    "paid",
    "brand_filed",
    "otp_pending",
    "brand_approved",
    "campaign_filed",
    "active",
)
#: Stages that hold the workspace's one registration slot.
OPEN_STAGES = ("checkout", *WORKING_STAGES, "needs_attention")
#: Approval evidence is refreshed well inside the send gate's max age (7 days by default).
EVIDENCE_REFRESH = timedelta(hours=12)

#: Telnyx /10dlc/enum/vertical. Anything else is refused at filing, after payment.
VERTICALS = frozenset(
    {
        "AGRICULTURE",
        "COMMUNICATION",
        "CONSTRUCTION",
        "EDUCATION",
        "ENERGY",
        "ENTERTAINMENT",
        "FINANCIAL",
        "GAMBLING",
        "GOVERNMENT",
        "HEALTHCARE",
        "HOSPITALITY",
        "HUMAN_RESOURCES",
        "INSURANCE",
        "LEGAL",
        "MANUFACTURING",
        "NGO",
        "POLITICAL",
        "POSTAL",
        "PROFESSIONAL",
        "REAL_ESTATE",
        "RETAIL",
        "TECHNOLOGY",
        "TRANSPORTATION",
    }
)
#: The standard ($10/month) use cases offered self-serve. Special use cases carry their own
#: carrier vetting and fees, so they stay with the team.
STANDARD_USECASES = frozenset(
    {"MIXED", "MARKETING", "CUSTOMER_CARE", "ACCOUNT_NOTIFICATION", "DELIVERY_NOTIFICATION", "2FA"}
)

_E164_US = re.compile(r"^\+1\d{10}$")
_OTP = re.compile(r"^\d{6}$")
PIN_SMS = "Your Ringlite texting registration code is @OTP_PIN@. It expires in 24 hours."
SUCCESS_SMS = "Thanks - your number is verified for business texting with Ringlite."


def quote(tier: str, *, brand_reused: bool = False) -> dict:
    """``brand_reused``: the business is already approved, so only the campaign is filed and
    the brand fee is neither charged nor refunded. No workspace discount ever applies to a
    10DLC charge (owner rule 2026-10-18)."""
    monthly = sell_cents(MONTHLY_CENTS[tier])
    brand = 0 if brand_reused else sell_cents(BRAND_FEE_CENTS)
    review = CAMPAIGN_REVIEW_SELL_CENTS
    service = SERVICE_FEE_CENTS
    return {
        "fee_tier": tier,
        "brand_fee_cents": brand,
        "campaign_review_cents": review,
        "service_fee_cents": service,
        "service_discount_cents": 0,
        "monthly_cents": monthly,
        "upfront_months": UPFRONT_MONTHS,
        "due_today_cents": (
            brand + review + service + UPFRONT_MONTHS * monthly
        ),
    }


def customer_quote(tier: str, *, brand_reused: bool = False) -> dict:
    """What the customer is shown: totals only, never the carrier / Ringlite split."""
    q = quote(tier, brand_reused=brand_reused)
    return {
        key: q[key]
        for key in ("fee_tier", "monthly_cents", "upfront_months", "due_today_cents")
    }


def tier_for(brand: Brand) -> str:
    return (
        "sole_proprietor" if (brand.entity_type or "").upper() == "SOLE_PROPRIETOR" else "standard"
    )


def public(
    reg: TenDlcRegistration,
    brand: Brand | None,
    campaign: Campaign | None,
) -> dict:
    return {
        "id": str(reg.id),
        "stage": reg.stage,
        "brand_id": str(reg.brand_id),
        "campaign_id": str(reg.campaign_id),
        "brand_status": brand.status if brand else None,
        "campaign_status": campaign.status if campaign else None,
        "checkout_url": reg.checkout_url if reg.stage == "checkout" else None,
        "otp_sent_at": reg.otp_sent_at.isoformat() if reg.otp_sent_at else None,
        "detail": reg.detail,
        **customer_quote(
            reg.fee_tier,
            brand_reused=bool((reg.filing or {}).get("brand_reused")),
        ),
    }


async def current(session, org_id) -> TenDlcRegistration | None:
    return (
        await session.execute(
            sa.select(TenDlcRegistration)
            .where(TenDlcRegistration.org_id == org_id)
            .order_by(TenDlcRegistration.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


# --------------------------------------------------------------------------------------
# Checkout
# --------------------------------------------------------------------------------------
def _clean_review(review: dict) -> dict:
    """The advisory check the customer saw, kept on the filing: bounded and string-only."""

    suggestions = [
        {str(k)[:64]: v[:500] for k, v in s.items() if isinstance(v, str)}
        for s in (review.get("suggestions") or [])[:20]
        if isinstance(s, dict)
    ]
    return {
        "suggestions": suggestions,
        "submitted_anyway": bool(review.get("submitted_anyway")),
        "at": datetime.now(timezone.utc).isoformat(),
    }


async def start_checkout(
    session,
    settings,
    org_id: uuid.UUID,
    *,
    brand_id: uuid.UUID,
    campaign_id: uuid.UUID,
    first_name: str,
    last_name: str,
    mobile_phone: str | None,
    assertions: dict[str, bool],
    sub_usecases: list[str] | None = None,
    customer_email: str | None = None,
    review: dict | None = None,
) -> TenDlcRegistration:
    """Validate everything the carrier will check, THEN take payment. A form error found
    after the customer paid would cost them a non-refundable filing."""
    from app.providers.telnyx.brand_payload import build_brand_payload
    from app.providers.telnyx.campaign_payload import build_campaign_payload
    from app.services.registration import (
        validate_brand_for_submission,
        validate_campaign_for_submission,
    )

    brand = await session.get(Brand, brand_id)
    campaign = await session.get(Campaign, campaign_id)
    if brand is None or campaign is None or brand.org_id != org_id or campaign.org_id != org_id:
        raise ValidationFailedError("Choose a business profile and a texting campaign first.")
    if campaign.brand_id != brand.id:
        raise ValidationFailedError("That campaign belongs to a different business profile.")

    existing = (
        await session.execute(
            sa.select(TenDlcRegistration)
            .where(
                TenDlcRegistration.org_id == org_id,
                TenDlcRegistration.stage.in_(OPEN_STAGES),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.stage == "checkout"
            and existing.brand_id == brand.id
            and existing.campaign_id == campaign.id
            and existing.checkout_url
        ):
            return existing
        if existing.stage != "checkout":
            raise ConflictError("Texting registration is already under way for this workspace.")
        # Superseded by a different choice: close the old payment page so it cannot be paid.
        if existing.checkout_id:
            stripe = stripe_client._stripe(settings)
            remote = await stripe_client._run_sync(
                stripe.checkout.Session.retrieve, existing.checkout_id
            )
            if remote.get("status") == "complete":
                raise ConflictError("Your earlier payment went through; it is being processed.")
            if remote.get("status") == "open":
                await stripe_client._run_sync(stripe.checkout.Session.expire, existing.checkout_id)
        existing.stage = "expired"

    refs = brand.carrier_refs or {}
    # An approved, carrier-registered business is reused: only a declined campaign is refiled.
    brand_reused = brand.status == "approved" and bool(refs.get("telnyx"))
    if not brand_reused and (brand.status not in ("draft", "submitted") or refs.get("telnyx")):
        raise ConflictError("This business profile has already been registered.")
    unfiled = campaign.status in ("draft", "submitted") and not (campaign.carrier_refs or {}).get(
        "telnyx"
    )
    if not unfiled and not (brand_reused and campaign.status == "rejected"):
        raise ConflictError("This campaign has already been registered.")

    tier = tier_for(brand)
    mobile = (mobile_phone or "").strip() or None
    if tier == "sole_proprietor":
        if not brand_reused and (not mobile or not _E164_US.match(mobile)):
            raise ValidationFailedError(
                "Enter the owner's US mobile number as +1 and 10 digits. The carrier texts a "
                "verification code to it."
            )
        # A sole proprietor campaign has exactly one use case at the carrier.
        campaign.use_case = "SOLE_PROPRIETOR"
    elif (campaign.use_case or "").upper() not in STANDARD_USECASES:
        raise ValidationFailedError(
            "Choose one of the standard texting use cases for this campaign."
        )
    if (brand.vertical or "").strip().upper() not in VERTICALS:
        raise ValidationFailedError("Choose your industry from the list.")

    validate_brand_for_submission(brand)
    validate_campaign_for_submission(campaign)
    filing = {
        "company_name": brand.name,
        "first_name": (first_name or "").strip(),
        "last_name": (last_name or "").strip(),
        "mobile_phone": mobile,
        "assertions": {k: bool(v) for k, v in (assertions or {}).items()},
        "sub_usecases": [str(s).strip().upper() for s in (sub_usecases or []) if s],
    }
    if brand_reused:
        filing["brand_reused"] = True
    if review:
        filing["review"] = _clean_review(review)
        audit_svc.record(
            session,
            org_id,
            action="tendlc.review_acknowledged",
            target_type="campaign",
            target_id=str(campaign.id),
            detail={
                "submitted_anyway": filing["review"]["submitted_anyway"],
                "suggestion_count": len(filing["review"]["suggestions"]),
            },
        )
    # Dry-run both carrier payloads: exactly what the job will send, minus the ids.
    if not brand_reused:
        build_brand_payload(
            brand,
            company_name=filing["company_name"],
            first_name=filing["first_name"] or None,
            last_name=filing["last_name"] or None,
            brand_relationship=BRAND_RELATIONSHIP,
            mobile_phone=mobile,
        )
    build_campaign_payload(
        campaign,
        telnyx_brand_id="pending",
        assertions=filing["assertions"],
        sub_usecases=filing["sub_usecases"],
    )

    stripe = stripe_client._stripe(settings)
    reg = TenDlcRegistration(
        id=uuid.uuid4(),
        org_id=org_id,
        brand_id=brand.id,
        campaign_id=campaign.id,
        fee_tier=tier,
        stage="checkout",
        filing=filing,
    )
    session.add(reg)
    await session.commit()

    fees = quote(tier, brand_reused=brand_reused)
    metadata = {"kind": "tendlc_fee", "registration_id": str(reg.id), "org_id": str(org_id)}
    base = settings.public_web_url.rstrip("/")
    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [
            {
                # The marked-up monthly fee (sell_cents), so a price change never needs a
                # new Stripe Price id in the settings.
                "price_data": {
                    "currency": "usd",
                    "unit_amount": fees["monthly_cents"],
                    "recurring": {"interval": "month"},
                    "product_data": {"name": "Texting registration, monthly"},
                },
                "quantity": 1,
            },
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": fees["due_today_cents"],
                    "product_data": {
                        "name": "Texting registration, including the first "
                        f"{UPFRONT_MONTHS} months",
                    },
                },
                "quantity": 1,
            },
        ],
        # The first 3 months are in today's one-time charge, so the monthly fee starts in
        # month four - the same schedule Telnyx bills us on.
        "subscription_data": {"trial_period_days": 30 * UPFRONT_MONTHS, "metadata": metadata},
        "metadata": metadata,
        "payment_method_options": stripe_client.THREE_DS_OPTIONS,
        "success_url": f"{base}/settings/messaging?texting=paid",
        "cancel_url": f"{base}/settings/messaging",
        # "-3ds": P44c changed the params; the old key would 400 for 24h after deploy.
        "idempotency_key": f"tendlc-{reg.id}-3ds-brand",
        **stripe_client.checkout_branding(settings),
    }
    if customer_email:
        params["customer_email"] = customer_email
    checkout = await stripe_client._run_sync(stripe.checkout.Session.create, **params)
    reg.checkout_id = checkout["id"]
    reg.checkout_url = checkout["url"]
    await session.commit()
    log.info("tendlc_checkout_created", org_id=str(org_id), tier=tier)
    return reg


async def handle_event(session, event: dict) -> bool:
    """Stripe events for 10DLC fees. Returns False for anything that is not ours."""
    obj = (event.get("data") or {}).get("object") or {}
    metadata = obj.get("metadata") or {}
    if metadata.get("kind") != "tendlc_fee":
        return False
    try:
        reg_id = uuid.UUID(metadata["registration_id"])
    except (KeyError, ValueError):
        log.warning("tendlc_event_bad_metadata", event_type=event.get("type"))
        return True
    reg = (
        await session.execute(
            sa.select(TenDlcRegistration)
            .where(TenDlcRegistration.id == reg_id)
            .with_for_update()
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()
    if reg is None:
        log.warning("tendlc_event_unknown_registration", registration_id=str(reg_id))
        return True
    set_org_context(session, reg.org_id)
    event_type = event.get("type")
    if event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("id") == reg.checkout_id and obj.get("payment_status") == "paid":
            if reg.stage == "checkout":
                reg.stage = "paid"
                reg.paid_at = datetime.now(timezone.utc)
                reg.subscription_id = obj.get("subscription")
                reg.detail = None
                log.info("tendlc_paid", registration_id=str(reg.id))
    elif event_type == "checkout.session.expired" and reg.stage == "checkout":
        if obj.get("id") == reg.checkout_id:
            reg.stage = "expired"
    await session.commit()
    return True


# --------------------------------------------------------------------------------------
# Background progression
# --------------------------------------------------------------------------------------
async def _registration_client(session, settings, client=None):
    from app.providers.telnyx.registration import TelnyxRegistrationClient
    from app.services import provider_accounts

    account = await provider_accounts.active_account_for(session, "telnyx")
    resolved = (
        settings if account is None else provider_accounts.settings_like_for(settings, account)
    )
    key = resolved.telnyx_api_key.get_secret_value().strip()
    if not key:
        raise FeatureUnavailableError("No Telnyx API key is configured")
    return TelnyxRegistrationClient(api_key=key, client=client)


def _telnyx_ref(record) -> str | None:
    value = (record.carrier_refs or {}).get("telnyx")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _record_evidence(record, *, approved: bool, source: str) -> None:
    from app.compliance import telnyx_approval

    carrier_id = _telnyx_ref(record)
    if carrier_id is None:
        return
    if not approved and record.status != "approved":
        return  # nothing to revoke
    telnyx_approval.apply_evidence(
        record,
        telnyx_approval.build_evidence(
            state=telnyx_approval.STATE_APPROVED if approved else telnyx_approval.STATE_REVOKED,
            carrier_id=carrier_id,
            checked_at=datetime.now(timezone.utc),
            source=source,
        ),
    )


def _attempted(record, marker_key: str) -> bool:
    refs = record.carrier_refs or {}
    return bool(refs.get(marker_key)) or bool(refs.get("telnyx"))


async def tick(session_factory, settings, *, client=None) -> dict[str, int]:
    """One pass over every registration with work to do. Each runs in its own session, so
    one failure never touches another."""
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    sa.select(TenDlcRegistration.id)
                    .where(TenDlcRegistration.stage.in_(WORKING_STAGES))
                    .execution_options(**{ALLOW_UNSCOPED_KEY: True})
                )
            )
            .scalars()
            .all()
        )
    counts = {"checked": 0, "advanced": 0, "failed": 0}
    for reg_id in rows:
        counts["checked"] += 1
        async with session_factory() as session:
            try:
                before, after = await advance(session, settings, reg_id, client=client)
                if after != before:
                    counts["advanced"] += 1
            except Exception:
                counts["failed"] += 1
                log.exception("tendlc_advance_failed", registration_id=str(reg_id))
    return counts


async def advance(session, settings, reg_id, *, client=None) -> tuple[str, str]:
    """Move one registration as far as the carrier allows right now."""
    reg = (
        await session.execute(
            sa.select(TenDlcRegistration)
            .where(TenDlcRegistration.id == reg_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    set_org_context(session, reg.org_id)
    start = reg.stage
    for _ in range(4):  # a few hops per pass, e.g. approved brand -> file campaign
        stage = reg.stage
        await _step(session, settings, reg, client)
        await session.commit()
        if reg.stage == stage:
            break
        await tendlc_notices.stage_changed(session, settings, reg, stage, reg.stage)
    return start, reg.stage


async def handle_carrier_event(session, settings, event, *, verified: bool, client=None) -> str:
    """10DLC phase 1: a Telnyx webhook is a TRIGGER. Record it on the workspace's timeline,
    keep the carrier's reason text, then let advance() ask Telnyx itself what changed."""
    from app.services import tendlc_events

    rows = (
        await session.execute(
            sa.select(TenDlcRegistration, Brand, Campaign)
            .join(Brand, Brand.id == TenDlcRegistration.brand_id)
            .join(Campaign, Campaign.id == TenDlcRegistration.campaign_id)
            .where(TenDlcRegistration.stage.notin_(("cancelled", "expired")))
            .order_by(TenDlcRegistration.created_at.desc())
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    reg = None
    if event.subject == "brand":
        reg = next((r for r, b, _ in rows if _telnyx_ref(b) == event.telnyx_id), None)
    elif event.subject == "campaign":
        reg = next((r for r, _, c in rows if _telnyx_ref(c) == event.telnyx_id), None)
    else:
        number = (
            await session.execute(
                sa.select(OrgNumber).where(OrgNumber.e164 == event.telnyx_id)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().first()
        if number is not None:
            reg = next((r for r, _, _ in rows if r.org_id == number.org_id
                        and r.stage == "active"), None)
    if reg is None:
        log.info("tendlc_event_unmatched", subject=event.subject, telnyx_id=event.telnyx_id)
        return "unmatched"
    reg_id, org_id = reg.id, reg.org_id
    set_org_context(session, org_id)
    row = await tendlc_events.record(
        session, org_id=org_id, registration_id=reg_id, event=event, verified=verified
    )
    if row is None:
        await session.commit()
        return "duplicate"
    kept = reg.rejection or {}
    if (
        event.subject in ("brand", "campaign")
        and tendlc_events.is_rejection(event)
        and (verified or not kept.get("verified"))
    ):
        reg.rejection = {
            "subject": event.subject,
            "reasons": list(event.reasons),
            "verified": verified,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    await session.commit()
    if reg.stage in WORKING_STAGES:
        await advance(session, settings, reg_id, client=client)
    return "recorded"


async def _step(session, settings, reg, client) -> None:
    from app.providers.telnyx.registration_status import (
        APPROVED,
        REJECTED,
        map_brand_status,
        map_campaign_status,
    )
    from app.services.registration import advance_status

    brand = await session.get(Brand, reg.brand_id)
    campaign = await session.get(Campaign, reg.campaign_id)
    filing = reg.filing or {}

    if reg.stage == "paid" and filing.get("brand_reused"):
        # The business is already approved: archive the declined campaign's carrier ids and
        # file a fresh campaign under it. No brand POST, no OTP.
        refs = dict(campaign.carrier_refs or {})
        old = refs.pop("telnyx", None)
        if old:
            refs["telnyx_previous"] = [*(refs.get("telnyx_previous") or []), old]
        refs.pop(_CAMPAIGN_MARKER, None)
        campaign.carrier_refs = refs
        campaign.status = "draft"  # a rejected campaign is terminal in advance_status
        campaign.last_error = None
        reg.stage = "brand_approved"
        reg.detail = None
        return

    if reg.stage == "paid":
        await _file(
            session,
            reg,
            brand,
            "telnyx_brand_filing",
            _file_brand(session, settings, brand, filing, client),
            done_stage="brand_filed",
        )
        return

    if reg.stage == "brand_filed" and reg.fee_tier == "sole_proprietor" and not reg.otp_sent_at:
        try:
            await send_otp(session, settings, reg, client=client)
        except Exception:
            log.exception("tendlc_otp_send_failed", registration_id=str(reg.id))
            reg.detail = "Sending your verification code; we will retry shortly."
        return

    if reg.stage in ("brand_filed", "otp_pending"):
        registration = await _registration_client(session, settings, client)
        try:
            payload = await registration.get_brand(_telnyx_ref(brand))
        finally:
            await registration.aclose()
        verdict = map_brand_status(payload)
        if verdict == APPROVED:
            advance_status(brand, "approved")
            _record_evidence(brand, approved=True, source="status_decision")
            reg.stage = "brand_approved"
            reg.detail = None
        elif verdict == REJECTED:
            from app.services.tendlc_events import rejection_text

            mine = (reg.rejection or {}).get("subject") == "brand"
            why = rejection_text(
                reg.rejection, "The carrier did not accept this business.", subject="brand"
            )
            advance_status(brand, "rejected", error=why[:500])
            reg.stage = "brand_rejected"
            refund = await _refund_unused(settings, reg)
            reg.detail = refund if not mine else f"{why} {refund}"
        return

    if reg.stage == "brand_approved":
        from app.services.telnyx_campaign_filing import file_campaign_with_telnyx

        await _file(
            session,
            reg,
            campaign,
            "telnyx_filing",
            file_campaign_with_telnyx(
                session,
                settings,
                campaign,
                assertions=filing.get("assertions") or {},
                sub_usecases=filing.get("sub_usecases") or None,
                client=client,
            ),
            done_stage="campaign_filed",
        )
        return

    if reg.stage == "campaign_filed":
        registration = await _registration_client(session, settings, client)
        try:
            payload = await registration.get_campaign(_telnyx_ref(campaign))
        finally:
            await registration.aclose()
        verdict = map_campaign_status(payload)
        if verdict == APPROVED:
            advance_status(campaign, "approved")
            _record_evidence(campaign, approved=True, source="status_decision")
            reg.evidence_checked_at = datetime.now(timezone.utc)
            reg.stage = "active"
            reg.detail = None
            await session.commit()
            await associate_numbers(session, settings, reg, campaign, client=client)
        elif verdict == REJECTED:
            from app.services.tendlc_events import rejection_text

            mine = (reg.rejection or {}).get("subject") == "campaign"
            why = rejection_text(
                reg.rejection, "The carriers did not accept this campaign.", subject="campaign"
            )
            advance_status(campaign, "rejected", error=why[:500])
            reg.stage = "campaign_rejected"
            stop = await _cancel_monthly(settings, reg)
            reg.detail = stop if not mine else f"{why} {stop}"
        return

    if reg.stage == "active":
        now = datetime.now(timezone.utc)
        await move_monthly_to_plan(session, settings, reg)
        checked = reg.evidence_checked_at
        if checked is not None and checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        if checked is None or now - checked >= EVIDENCE_REFRESH:
            registration = await _registration_client(session, settings, client)
            try:
                payload = await registration.get_campaign(_telnyx_ref(campaign))
            finally:
                await registration.aclose()
            _record_evidence(
                campaign, approved=map_campaign_status(payload) == APPROVED, source="refresh"
            )
            reg.evidence_checked_at = now
            await session.commit()
        await associate_numbers(session, settings, reg, campaign, client=client)


async def _file_brand(session, settings, brand, filing, client):
    from app.services.telnyx_brand_filing import file_brand_with_telnyx

    return await file_brand_with_telnyx(
        session,
        settings,
        brand,
        company_name=filing.get("company_name"),
        first_name=filing.get("first_name") or None,
        last_name=filing.get("last_name") or None,
        brand_relationship=BRAND_RELATIONSHIP,
        mobile_phone=filing.get("mobile_phone"),
        client=client,
    )


async def _file(session, reg, record, marker_key, call, *, done_stage) -> None:
    """Run one carrier filing. A failure BEFORE the attempt marker was written (no key,
    Telnyx unreachable before sending) is retried on the next pass. A failure after it is
    ambiguous - Telnyx may have accepted it - and the filing service refuses to send twice,
    so the registration stops for an operator instead of paying a second fee."""
    reg_id = reg.id
    try:
        await call
    except Exception as error:
        await session.rollback()
        reg = await session.get(TenDlcRegistration, reg_id)
        await session.refresh(record)
        if _attempted(record, marker_key):
            reg.stage = "needs_attention"
            reg.detail = (
                "Your registration is with our team to finish. You will not be charged again."
            )
            log.error(
                "tendlc_filing_needs_attention",
                registration_id=str(reg_id),
                error_type=type(error).__name__,
            )
        else:
            reg.detail = "Submitting to the carrier; we will retry shortly."
            log.warning("tendlc_filing_retry_later", registration_id=str(reg_id), error=str(error))
        return
    reg.stage = done_stage
    reg.detail = None


async def send_otp(session, settings, reg, *, client=None) -> None:
    """Text the sole proprietor's verification PIN (again). Each PIN lives 24 hours."""
    brand = await session.get(Brand, reg.brand_id)
    brand_ref = _telnyx_ref(brand)
    if reg.fee_tier != "sole_proprietor" or brand_ref is None:
        raise ConflictError("This registration does not need a mobile verification code.")
    registration = await _registration_client(session, settings, client)
    try:
        await registration.trigger_sms_otp(brand_ref, pin_sms=PIN_SMS, success_sms=SUCCESS_SMS)
    finally:
        await registration.aclose()
    reg.otp_sent_at = datetime.now(timezone.utc)
    reg.stage = "otp_pending"
    reg.detail = None


async def verify_otp(session, settings, reg, pin: str, *, client=None) -> None:
    pin = (pin or "").strip()
    if not _OTP.match(pin):
        raise ValidationFailedError("Enter the 6-digit code from the text message.")
    if reg.stage != "otp_pending":
        raise ConflictError("There is no verification code waiting for this registration.")
    brand = await session.get(Brand, reg.brand_id)
    registration = await _registration_client(session, settings, client)
    try:
        await registration.verify_sms_otp(_telnyx_ref(brand), pin)
    except ValidationFailedError:
        raise ValidationFailedError(
            "That code did not match or has expired. Check the text, or send a new code."
        ) from None
    finally:
        await registration.aclose()
    reg.stage = "brand_filed"  # the next pass reads the brand's verified identity
    reg.detail = None
    await session.commit()
    await tendlc_notices.stage_changed(session, settings, reg, "otp_pending", "brand_filed")
    await advance(session, settings, reg.id, client=client)


async def associate_numbers(session, settings, reg, campaign, *, client=None) -> int:
    """Put the workspace's texting-capable numbers on its approved campaign. A sole
    proprietor campaign takes exactly one number at the carrier."""
    from app.services.telnyx_number_association import associate_number_with_telnyx

    if campaign.status != "approved" or not _telnyx_ref(campaign):
        return 0
    numbers = (
        (
            await session.execute(
                sa.select(OrgNumber)
                .where(
                    OrgNumber.org_id == reg.org_id,
                    OrgNumber.carrier == "telnyx",
                    OrgNumber.number_type == "local",
                    OrgNumber.status == "active",
                    OrgNumber.is_active.is_(True),
                )
                .order_by(OrgNumber.created_at)
            )
        )
        .scalars()
        .all()
    )
    if reg.fee_tier == "sole_proprietor":
        if any(n.campaign_id == campaign.id for n in numbers):
            return 0
        numbers = numbers[:1]
    done = 0
    reg_id, reg_org_id = reg.id, reg.org_id
    for number in numbers:
        if (
            number.campaign_id is not None
            or (number.provisioning or {}).get("telnyx_campaign_assignment")
            or (number.provisioning or {}).get("telnyx_campaign_move")
        ):
            continue
        number_id, number_e164 = number.id, number.e164
        try:
            await associate_number_with_telnyx(session, settings, number, campaign, client=client)
            done += 1
        except Exception as exc:
            await session.rollback()
            log.exception("tendlc_number_association_failed", number_id=str(number_id))
            from app.services import tendlc_events

            try:
                set_org_context(session, reg_org_id)
                already = (
                    await session.execute(
                        sa.select(tendlc_events.TenDlcEvent.id)
                        .where(
                            tendlc_events.TenDlcEvent.registration_id == reg_id,
                            tendlc_events.TenDlcEvent.subject == "number",
                            tendlc_events.TenDlcEvent.telnyx_id == str(number_e164),
                            tendlc_events.TenDlcEvent.event_type == "local.number.assignment",
                            tendlc_events.TenDlcEvent.status == "FAILED",
                        )
                        .limit(1)
                    )
                ).first()
                if already is not None:
                    continue
                recorded = await tendlc_events.record(
                    session,
                    org_id=reg_org_id,
                    registration_id=reg_id,
                    event=tendlc_events.CarrierEvent(
                        telnyx_event_id=None,
                        event_type="local.number.assignment",
                        subject="number",
                        telnyx_id=str(number_e164),
                        payload_type="ASSIGNMENT",
                        status="FAILED",
                        reasons=[str(exc)[:500] or type(exc).__name__],
                        raw={},
                    ),
                    verified=True,
                )
                if recorded is not None:
                    from app.services import card_risk

                    await card_risk.open_alert(
                        session,
                        reg_org_id,
                        "tendlc_number_failed",
                        {
                            "registration_id": str(reg_id),
                            "number": str(number_e164),
                            "error": str(exc)[:500],
                        },
                    )
                await session.commit()
                if recorded is not None:
                    await session.refresh(reg)
                    await tendlc_notices.number_failed(
                        session, settings, reg, str(number_e164), str(exc)
                    )
            except Exception:
                log.exception("tendlc_assignment_event_failed", registration_id=str(reg_id))
    return done


async def associate_new_numbers(session, settings, org_id) -> None:
    """After a purchase: put new numbers on the workspace's approved campaign right away,
    not at the next background pass. Never raises."""
    try:
        reg = (
            await session.execute(
                sa.select(TenDlcRegistration)
                .where(TenDlcRegistration.org_id == org_id, TenDlcRegistration.stage == "active")
                .limit(1)
            )
        ).scalar_one_or_none()
        if reg is None:
            return
        telnyx_approved = [
            c
            for c in (
                await session.execute(
                    sa.select(Campaign).where(
                        Campaign.org_id == org_id, Campaign.status == "approved"
                    )
                )
            )
            .scalars()
            .all()
            if _telnyx_ref(c)
        ]
        if len(telnyx_approved) > 1:
            # Never guess which campaign a new number belongs on: the customer picks.
            log.info("tendlc_numbers_need_choice", org_id=str(org_id))
            return
        campaign = await session.get(Campaign, reg.campaign_id)
        await associate_numbers(session, settings, reg, campaign)
    except Exception:
        log.exception("tendlc_associate_new_numbers_failed", org_id=str(org_id))


# --------------------------------------------------------------------------------------
# Operator recovery for an interrupted filing
# --------------------------------------------------------------------------------------
#: A filing interrupted this recently may still be landing at the carrier; never guess yet.
RECONCILE_AFTER = timedelta(minutes=10)
_BRAND_MARKER = "telnyx_brand_filing"
_CAMPAIGN_MARKER = "telnyx_filing"


def _when(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _created(record: dict) -> datetime | None:
    return _when(record.get("createdAt") or record.get("createDate") or record.get("created_at"))


def _attempted_at(marker: dict) -> datetime:
    attempted = _when(marker.get("attempted_at"))
    if attempted is None:
        raise ConflictError("The interrupted filing has no timestamp; reconcile it by hand.")
    if datetime.now(timezone.utc) - attempted < RECONCILE_AFTER:
        raise ConflictError("That filing was attempted minutes ago. Try again in ten minutes.")
    return attempted


def _made_after(record: dict, attempted: datetime) -> bool:
    created = _created(record)
    return created is None or created >= attempted - timedelta(minutes=5)


async def reconcile(session, settings, reg_id, *, client=None) -> dict:
    """Operator: settle a filing that was interrupted after it was sent.

    Asks Telnyx whether the record was created. Exactly one match: link it and carry on.
    None: the submission never landed, so clear the attempt marker and let the job file it
    again. More than one: stop - that needs a person. Never files anything itself.
    """
    from app.services.registration import advance_status

    reg = (
        await session.execute(
            sa.select(TenDlcRegistration)
            .where(TenDlcRegistration.id == reg_id)
            .with_for_update()
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    set_org_context(session, reg.org_id)
    if reg.stage != "needs_attention":
        raise ConflictError(f"This registration is {reg.stage}; there is nothing to reconcile.")
    brand = await session.get(Brand, reg.brand_id)
    campaign = await session.get(Campaign, reg.campaign_id)
    registration = await _registration_client(session, settings, client)
    try:
        if _telnyx_ref(brand) is None:
            marker = (brand.carrier_refs or {}).get(_BRAND_MARKER) or {}
            attempted = _attempted_at(marker)
            found = await registration.list_brands(displayName=brand.name)
            matches = [
                b
                for b in found
                if (b.get("email") or "").lower() == (brand.email or "").lower()
                and _made_after(b, attempted)
            ]
            refs = dict(brand.carrier_refs or {})
            if len(matches) == 1:
                refs["telnyx"] = str(matches[0].get("brandId") or matches[0].get("id"))
                refs.pop(_BRAND_MARKER, None)
                brand.carrier_refs = refs
                advance_status(brand, "submitted")
                reg.stage = "brand_filed"
                outcome = "linked the brand Telnyx created"
            elif not matches:
                refs.pop(_BRAND_MARKER, None)
                brand.carrier_refs = refs
                reg.stage = "paid"
                outcome = "Telnyx has no such brand; it will be filed again"
            else:
                reg.detail = "Several matching brands exist at the carrier; resolve by hand."
                await session.commit()
                raise ConflictError(reg.detail)
        elif _telnyx_ref(campaign) is None:
            marker = (campaign.carrier_refs or {}).get(_CAMPAIGN_MARKER) or {}
            attempted = _attempted_at(marker)
            found = await registration.list_campaigns(_telnyx_ref(brand))
            reference = marker.get("reference_id")
            matches = [c for c in found if reference and c.get("referenceId") == reference] or [
                c for c in found if _made_after(c, attempted)
            ]
            refs = dict(campaign.carrier_refs or {})
            if len(matches) == 1:
                refs["telnyx"] = str(matches[0].get("campaignId") or matches[0].get("id"))
                refs[_CAMPAIGN_MARKER] = {**marker, "state": "submitted"}
                campaign.carrier_refs = refs
                advance_status(campaign, "submitted")
                reg.stage = "campaign_filed"
                outcome = "linked the campaign Telnyx created"
            elif not matches:
                refs.pop(_CAMPAIGN_MARKER, None)
                campaign.carrier_refs = refs
                reg.stage = "brand_approved"
                outcome = "Telnyx has no such campaign; it will be filed again"
            else:
                reg.detail = "Several matching campaigns exist at the carrier; resolve by hand."
                await session.commit()
                raise ConflictError(reg.detail)
        else:
            reg.stage = "campaign_filed"
            outcome = "both are filed; resuming"
    finally:
        await registration.aclose()
    reg.detail = None
    await session.commit()
    log.info("tendlc_reconciled", registration_id=str(reg.id), outcome=outcome)
    await advance(session, settings, reg.id, client=client)
    return {"stage": (await session.get(TenDlcRegistration, reg.id)).stage, "outcome": outcome}


async def cancel(session, settings, reg_id) -> str:
    """Operator: stop a registration for good and refund whatever the carrier never
    charged us for. Nothing filed: everything. Brand filed only: the review and the three
    months. Campaign filed: nothing (the carrier has billed it); the monthly fee stops."""
    reg = (
        await session.execute(
            sa.select(TenDlcRegistration)
            .where(TenDlcRegistration.id == reg_id)
            .with_for_update()
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    set_org_context(session, reg.org_id)
    if reg.stage in ("active", "cancelled", "expired", "brand_rejected", "campaign_rejected"):
        raise ConflictError(f"This registration is {reg.stage}; it cannot be cancelled here.")
    brand = await session.get(Brand, reg.brand_id)
    campaign = await session.get(Campaign, reg.campaign_id)
    reused = bool((reg.filing or {}).get("brand_reused"))
    fees = quote(reg.fee_tier, brand_reused=reused)
    if _telnyx_ref(brand) is None and not (brand.carrier_refs or {}).get(_BRAND_MARKER):
        amount = fees["due_today_cents"]
    elif (reused and reg.stage == "paid") or (
        _telnyx_ref(campaign) is None and not (campaign.carrier_refs or {}).get(_CAMPAIGN_MARKER)
    ):
        # (A reused registration still at "paid" holds the declined campaign's old ref.)
        amount = fees["campaign_review_cents"] + UPFRONT_MONTHS * fees["monthly_cents"]
    else:
        amount = 0
    detail = "Cancelled by our team."
    await _drop_plan_item(settings, reg)
    if reg.subscription_id:
        stripe = stripe_client._stripe(settings)
        subscription = await stripe_client._run_sync(
            stripe.Subscription.retrieve, reg.subscription_id
        )
        if amount:
            payments = await stripe_client._run_sync(
                stripe.InvoicePayment.list, invoice=subscription["latest_invoice"], status="paid"
            )
            paid = next(
                (
                    p
                    for p in payments.get("data", [])
                    if (p.get("payment") or {}).get("payment_intent")
                ),
                None,
            )
            intent = paid["payment"]["payment_intent"] if paid else None
            if paid and isinstance(paid.get("amount_paid"), int):
                # A workspace discount lowered what was charged: never refund more than that.
                amount = min(amount, paid["amount_paid"])
            if intent:
                await stripe_client._run_sync(
                    stripe.Refund.create,
                    payment_intent=intent,
                    amount=amount,
                    metadata={"kind": "tendlc_refund", "registration_id": str(reg.id)},
                    idempotency_key=f"tendlc-cancel-refund-{reg.id}",
                )
                detail = f"Cancelled by our team; ${amount / 100:.2f} refunded."
            else:
                detail = "Cancelled; no card payment was found to refund - refund it in Stripe."
        if subscription["status"] != "canceled":
            await stripe_client._run_sync(
                stripe.Subscription.cancel,
                reg.subscription_id,
                idempotency_key=f"tendlc-cancel-{reg.id}",
            )
    elif reg.stage == "checkout" and reg.checkout_id:
        stripe = stripe_client._stripe(settings)
        remote = await stripe_client._run_sync(stripe.checkout.Session.retrieve, reg.checkout_id)
        if remote.get("status") == "open":
            await stripe_client._run_sync(stripe.checkout.Session.expire, reg.checkout_id)
    reg.stage = "cancelled"
    reg.detail = detail
    await session.commit()
    log.info("tendlc_cancelled", registration_id=str(reg.id), refunded_cents=amount)
    return detail


# --------------------------------------------------------------------------------------
# Money on the way out
# --------------------------------------------------------------------------------------
async def _drop_plan_item(settings, reg) -> None:
    """The monthly fee moved onto the plan subscription: take that item off it. The plan
    itself is untouched, and nothing is prorated back."""
    item_id = (reg.filing or {}).get("monthly_item_id")
    if not item_id:
        return
    stripe = stripe_client._stripe(settings)
    await stripe_client._run_sync(
        stripe.SubscriptionItem.delete, item_id, proration_behavior="none"
    )
    reg.filing = {k: v for k, v in reg.filing.items() if k not in ("monthly_item_id", "monthly_on")}


async def _cancel_monthly(settings, reg) -> str:
    """The carrier refused the campaign: stop the monthly fee. The one-time fees paid for
    a review that happened, so they are not refunded."""
    await _drop_plan_item(settings, reg)
    if reg.subscription_id and not (reg.filing or {}).get("own_sub_cancelled"):
        stripe = stripe_client._stripe(settings)
        await stripe_client._run_sync(
            stripe.Subscription.cancel,
            reg.subscription_id,
            idempotency_key=f"tendlc-cancel-{reg.id}",
        )
    return (
        "The carriers did not approve this campaign, so the monthly fee has been cancelled. "
        "Review the campaign details and start a new registration."
    )


async def _refund_unused(settings, reg) -> str:
    """The carrier refused the business before any campaign was filed: refund what was
    never spent (the campaign review and the three months) and stop the monthly fee."""
    reused = bool((reg.filing or {}).get("brand_reused"))
    fees = quote(reg.fee_tier, brand_reused=reused)
    amount = fees["campaign_review_cents"] + UPFRONT_MONTHS * fees["monthly_cents"]
    if not reg.subscription_id:
        return "The carrier did not accept this business profile."
    stripe = stripe_client._stripe(settings)
    subscription = await stripe_client._run_sync(stripe.Subscription.retrieve, reg.subscription_id)
    payments = await stripe_client._run_sync(
        stripe.InvoicePayment.list, invoice=subscription["latest_invoice"], status="paid"
    )
    intent = next(
        (
            p["payment"]["payment_intent"]
            for p in payments.get("data", [])
            if (p.get("payment") or {}).get("payment_intent")
        ),
        None,
    )
    if subscription["status"] != "canceled":
        await stripe_client._run_sync(
            stripe.Subscription.cancel,
            reg.subscription_id,
            idempotency_key=f"tendlc-cancel-{reg.id}",
        )
    if intent:
        await stripe_client._run_sync(
            stripe.Refund.create,
            payment_intent=intent,
            amount=amount,
            metadata={"kind": "tendlc_refund", "registration_id": str(reg.id)},
            idempotency_key=f"tendlc-refund-{reg.id}",
        )
    else:
        log.error("tendlc_refund_no_payment", registration_id=str(reg.id))
    kept = fees["brand_fee_cents"] + SERVICE_FEE_CENTS
    return (
        f"The carrier did not accept this business profile. ${amount / 100:.2f} was refunded; "
        f"${kept / 100:.2f} for the registration filing is "
        "not refundable."
    )


# --------------------------------------------------------------------------------------
# The monthly fee on the plan subscription
# --------------------------------------------------------------------------------------
_MONTHLY_PRODUCT_NAME = "Texting registration, monthly"
_monthly_product_id: str | None = None
_MOVE_KEYS = (
    "monthly_item_id",
    "monthly_on",
    "own_sub_cancelled",
    "monthly_cancelling",
    "monthly_window",
    "orphan_alerted",
)


async def _monthly_product(stripe) -> str:
    """One shared Stripe product for the item. Found by metadata first (idempotency keys
    expire after 24h, so a plain create would pile up products), cached per process."""
    global _monthly_product_id
    if _monthly_product_id is None:
        found = await stripe_client._run_sync(
            stripe.Product.search, query="metadata['kind']:'tendlc_monthly' AND active:'true'"
        )
        data = (found or {}).get("data") or []
        if data:
            _monthly_product_id = data[0]["id"]
        else:
            product = await stripe_client._run_sync(
                stripe.Product.create,
                name=_MONTHLY_PRODUCT_NAME,
                metadata={"kind": "tendlc_monthly"},
                idempotency_key="tendlc-monthly-product",
            )
            _monthly_product_id = product["id"]
    return _monthly_product_id


def _has_discount(subscription: dict) -> bool:
    """A subscription-level coupon would also reduce the 10DLC item. Item-level coupons (how
    workspace plan discounts are applied) sit on the other items and do not."""
    return bool(subscription.get("discount") or subscription.get("discounts"))


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _alert_orphan(session, reg, detail: dict) -> None:
    from app.services import card_risk

    await card_risk.open_alert(
        session,
        reg.org_id,
        "tendlc_monthly_orphan",
        {"registration_id": str(reg.id), **detail},
    )


async def move_monthly_to_plan(session, settings, reg) -> bool:
    """Once the three prepaid months run out, bill the monthly fee on the workspace's
    monthly plan subscription (same renewal invoice) instead of the 10DLC's own one.

    From one day before the own subscription's trial ends: the own subscription (still
    trialing, so it has never charged) is cancelled first, then the item is created with
    prorations. If the item create fails after the cancel it is retried every pass, and
    ops are alerted when it still fails a day after the window opened. Only an ACTIVE,
    undiscounted MONTHLY plan receives it (a coupon would sell the fee below cost); anything
    else leaves the standalone subscription billing alone. True when the fee is on the plan."""
    filing = reg.filing or {}
    if filing.get("monthly_standalone") or filing.get("monthly_item_id"):
        return bool(filing.get("monthly_item_id"))
    if reg.paid_at is None or not reg.subscription_id:
        return False
    now = datetime.now(timezone.utc)
    if not filing.get("own_sub_cancelled") and not filing.get("monthly_cancelling"):
        # Cheap guard before any Stripe call: the trial is 30 * UPFRONT_MONTHS days.
        if now < _as_utc(reg.paid_at) + timedelta(days=30 * UPFRONT_MONTHS - 3):
            return False

    try:
        stripe = stripe_client._stripe(settings)
        if not filing.get("own_sub_cancelled"):
            own = await stripe_client._run_sync(stripe.Subscription.retrieve, reg.subscription_id)
            if own["status"] == "canceled" and filing.get("monthly_cancelling"):
                # Our cancel reached Stripe but we never saved that: carry on to the item.
                filing = {**filing, "own_sub_cancelled": True}
                reg.filing = filing
                await session.commit()
                plan_sub = await _plan_target(session, stripe, reg)
                if plan_sub is None:
                    await _late_orphan(session, reg, filing, now, "no eligible plan subscription")
                    return False
            elif own["status"] != "trialing":
                # Already billing (or gone): moving now would double charge this period. A
                # subscription that bills nothing must never be silently called standalone.
                if own["status"] not in ("active", "past_due"):
                    await _alert_orphan(
                        session, reg, {"why": "own subscription is " + str(own["status"])}
                    )
                reg.filing = {**filing, "monthly_standalone": True}
                await session.commit()
                return False
            else:
                plan_sub = await _move_cancel_own(session, stripe, reg, filing, own, now)
                if plan_sub is None:
                    return False
                filing = reg.filing
        else:
            plan_sub = await _plan_target(session, stripe, reg)
            if plan_sub is None:
                await _late_orphan(session, reg, filing, now, "no eligible plan subscription")
                return False
        try:
            item = await stripe_client._run_sync(
                stripe.SubscriptionItem.create,
                subscription=plan_sub,
                price_data={
                    "currency": "usd",
                    "unit_amount": quote(reg.fee_tier)["monthly_cents"],
                    "recurring": {"interval": "month"},
                    "product": await _monthly_product(stripe),
                },
                quantity=1,
                metadata={"kind": "tendlc_monthly", "registration_id": str(reg.id)},
                proration_behavior="create_prorations",
                idempotency_key=f"tendlc-monthly-{reg.id}-{plan_sub}",
            )
        except Exception:
            await _late_orphan(session, reg, filing, now, "item create failed")
            raise
        reg.filing = {**filing, "monthly_item_id": item["id"], "monthly_on": plan_sub}
        await session.commit()
        log.info("tendlc_monthly_moved", registration_id=str(reg.id), plan=plan_sub)
        return True
    except Exception:
        log.exception("tendlc_monthly_move_failed", registration_id=str(reg.id))
        return False


async def _move_cancel_own(session, stripe, reg, filing, own, now) -> str | None:
    """Window check, plan check, then cancel the own (trialing) subscription. The intent is
    committed BEFORE the cancel so a lost response can be recognised on the next pass."""
    opens = datetime.fromtimestamp(int(own["trial_end"]), timezone.utc) - timedelta(days=1)
    if now < opens:
        return None
    plan_sub = await _plan_target(session, stripe, reg)
    if plan_sub is None:
        return None
    reg.filing = {
        **filing,
        "monthly_cancelling": plan_sub,
        "monthly_window": opens.isoformat(),
    }
    await session.commit()
    await stripe_client._run_sync(
        stripe.Subscription.cancel,
        reg.subscription_id,
        prorate=False,
        idempotency_key=f"tendlc-move-cancel-{reg.id}",
    )
    reg.filing = {**reg.filing, "own_sub_cancelled": True}
    await session.commit()
    return plan_sub


async def _plan_target(session, stripe, reg) -> str | None:
    """The plan subscription that may carry the fee: active, monthly, no coupon."""
    from app.services import plan_billing

    ent = await plan_billing.entitlement(session, reg.org_id)
    if ent is None or ent.interval != plan_billing.MONTH or ent.subscription.status != "active":
        return None
    plan_sub = ent.subscription.stripe_subscription_id
    remote = await stripe_client._run_sync(stripe.Subscription.retrieve, plan_sub)
    if _has_discount(remote):
        return None
    customer = remote.get("customer")
    if isinstance(customer, str):
        customer = await stripe_client._run_sync(stripe.Customer.retrieve, customer)
    if isinstance(customer, dict) and (customer.get("discount") or customer.get("discounts")):
        return None
    return plan_sub


async def _late_orphan(session, reg, filing: dict, now: datetime, why: str) -> None:
    """The own subscription is cancelled but the item is not on a plan: after a day of
    failed retries, tell ops the campaign is unbilled (once)."""
    window = filing.get("monthly_window")
    if not window or filing.get("orphan_alerted"):
        return
    if now < datetime.fromisoformat(window) + timedelta(days=1):
        return
    await _alert_orphan(session, reg, {"why": why})
    reg.filing = {**filing, "orphan_alerted": True}
    await session.commit()


async def restore_standalone_monthly(session, settings, org_id, plan_sub_id, plan_sub) -> None:
    """The plan subscription carrying a 10DLC monthly item was deleted: give each active
    registration on it its own subscription again, or tell ops it is now unbilled.
    `plan_sub` is the deleted subscription as Stripe reports it."""
    regs = (
        (
            await session.execute(
                sa.select(TenDlcRegistration)
                .where(TenDlcRegistration.org_id == org_id, TenDlcRegistration.stage == "active")
                .with_for_update()
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )
    for reg in regs:
        filing = reg.filing or {}
        if filing.get("monthly_on") != plan_sub_id:
            continue
        if not (filing.get("own_sub_cancelled") and filing.get("monthly_item_id")):
            continue
        try:
            stripe = stripe_client._stripe(settings)
            own = await stripe_client._run_sync(stripe.Subscription.retrieve, reg.subscription_id)
            if own["status"] != "canceled":
                # The own subscription still bills: a second one would charge twice. The
                # item went with the deleted plan, so only the bookkeeping is cleared.
                reg.filing = {k: v for k, v in filing.items() if k not in _MOVE_KEYS}
                continue
            customer = plan_sub.get("customer")
            if isinstance(customer, dict):
                customer = customer.get("id")
            method = plan_sub.get("default_payment_method")
            if isinstance(method, dict):
                method = method.get("id")
            if not method and customer:
                remote = await stripe_client._run_sync(stripe.Customer.retrieve, customer)
                method = ((remote or {}).get("invoice_settings") or {}).get(
                    "default_payment_method"
                )
                if isinstance(method, dict):
                    method = method.get("id")
            if not customer or not method:
                raise ValueError("no customer or payment method to bill")
            kwargs: dict = {}
            period_end = plan_sub.get("current_period_end")
            if not period_end:
                data = (plan_sub.get("items") or {}).get("data") or []
                period_end = data[0].get("current_period_end") if data else None
            # The item already billed the workspace up to the end of this period.
            if period_end and int(period_end) > datetime.now(timezone.utc).timestamp() + 3600:
                kwargs["trial_end"] = int(period_end)
            sub = await stripe_client._run_sync(
                stripe.Subscription.create,
                customer=customer,
                default_payment_method=method,
                items=[
                    {
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": quote(reg.fee_tier)["monthly_cents"],
                            "recurring": {"interval": "month"},
                            "product": await _monthly_product(stripe),
                        },
                        "quantity": 1,
                    }
                ],
                metadata={"kind": "tendlc_fee", "registration_id": str(reg.id)},
                idempotency_key=f"tendlc-monthly-restore-{reg.id}-{plan_sub_id}",
                **kwargs,
            )
            reg.subscription_id = sub["id"]
            reg.filing = {k: v for k, v in filing.items() if k not in _MOVE_KEYS}
            log.info("tendlc_monthly_restored", registration_id=str(reg.id))
            if sub.get("status") not in ("active", "trialing"):
                await _alert_orphan(
                    session,
                    reg,
                    {"plan_subscription": plan_sub_id, "status": sub.get("status")},
                )
        except Exception:
            log.exception("tendlc_monthly_orphan", registration_id=str(reg.id))
            await _alert_orphan(session, reg, {"plan_subscription": plan_sub_id})
