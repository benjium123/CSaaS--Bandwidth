"""Self-serve 10DLC: the customer pays the carrier fees at cost, then this drives filing.

The carrier writes stay in the existing services (telnyx_brand_filing,
telnyx_campaign_filing, telnyx_number_association), with every guard they already have:
one attempt per record, a durable marker committed before each POST, never an automatic
retry of an ambiguous write. This module only decides WHEN to call them, and turns the
carrier's own verdict into the local status and approval evidence that the send gate reads.
Nothing here marks anything approved that Telnyx has not reported as approved.

Fees are Telnyx's, passed through at cost (support.telnyx.com "10DLC Fees and Charges"):
brand registration $4.50 and campaign review $15 once, plus the monthly campaign fee,
which Telnyx bills three months upfront. So checkout takes the one-time fees plus three
months today, and the Stripe subscription starts charging the monthly fee from month four.
On top of the carrier fees, Ringlite charges a one-time SERVICE_FEE_CENTS registration fee
(its commission). The customer is shown totals only, never the split.
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
from app.services import stripe_client

log = structlog.get_logger("tendlc")

BRAND_FEE_CENTS = 450
CAMPAIGN_REVIEW_CENTS = 1500
#: Ringlite's commission per registration, on top of the carrier fees. Not refunded if the
#: carrier refuses the business (the filing work was done).
SERVICE_FEE_CENTS = 500
UPFRONT_MONTHS = 3
MONTHLY_CENTS = {"standard": 1000, "sole_proprietor": 200}
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


def quote(tier: str) -> dict:
    monthly = MONTHLY_CENTS[tier]
    return {
        "fee_tier": tier,
        "brand_fee_cents": BRAND_FEE_CENTS,
        "campaign_review_cents": CAMPAIGN_REVIEW_CENTS,
        "service_fee_cents": SERVICE_FEE_CENTS,
        "monthly_cents": monthly,
        "upfront_months": UPFRONT_MONTHS,
        "due_today_cents": (
            BRAND_FEE_CENTS + CAMPAIGN_REVIEW_CENTS + SERVICE_FEE_CENTS + UPFRONT_MONTHS * monthly
        ),
    }


def tier_for(brand: Brand) -> str:
    return (
        "sole_proprietor" if (brand.entity_type or "").upper() == "SOLE_PROPRIETOR" else "standard"
    )


def public(reg: TenDlcRegistration, brand: Brand | None, campaign: Campaign | None) -> dict:
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
        **quote(reg.fee_tier),
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
    if brand.status not in ("draft", "submitted") or refs.get("telnyx"):
        raise ConflictError("This business profile has already been registered.")
    if campaign.status not in ("draft", "submitted") or (campaign.carrier_refs or {}).get("telnyx"):
        raise ConflictError("This campaign has already been registered.")

    tier = tier_for(brand)
    mobile = (mobile_phone or "").strip() or None
    if tier == "sole_proprietor":
        if not mobile or not _E164_US.match(mobile):
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
    # Dry-run both carrier payloads: exactly what the job will send, minus the ids.
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

    price_id = getattr(settings, PRICE_SETTING[tier], "")
    if not price_id:
        raise FeatureUnavailableError("Texting registration is being configured.")
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

    fees = quote(tier)
    metadata = {"kind": "tendlc_fee", "registration_id": str(reg.id), "org_id": str(org_id)}
    base = settings.public_web_url.rstrip("/")
    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [
            {"price": price_id, "quantity": 1},
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
        "success_url": f"{base}/settings/messaging?texting=paid",
        "cancel_url": f"{base}/settings/messaging",
        "idempotency_key": f"tendlc-{reg.id}",
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
    return start, reg.stage


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
            advance_status(brand, "rejected", error="The carrier did not accept this business.")
            reg.stage = "brand_rejected"
            reg.detail = await _refund_unused(settings, reg)
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
            advance_status(campaign, "rejected", error="The carriers did not accept this campaign.")
            reg.stage = "campaign_rejected"
            reg.detail = await _cancel_monthly(settings, reg)
        return

    if reg.stage == "active":
        now = datetime.now(timezone.utc)
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
    for number in numbers:
        if number.campaign_id is not None or (number.provisioning or {}).get(
            "telnyx_campaign_assignment"
        ):
            continue
        try:
            await associate_number_with_telnyx(session, settings, number, campaign, client=client)
            done += 1
        except Exception:
            await session.rollback()
            log.exception("tendlc_number_association_failed", number_id=str(number.id))
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
        campaign = await session.get(Campaign, reg.campaign_id)
        await associate_numbers(session, settings, reg, campaign)
    except Exception:
        log.exception("tendlc_associate_new_numbers_failed", org_id=str(org_id))


# --------------------------------------------------------------------------------------
# Money on the way out
# --------------------------------------------------------------------------------------
async def _cancel_monthly(settings, reg) -> str:
    """The carrier refused the campaign: stop the monthly fee. The one-time fees paid for
    a review that happened, so they are not refunded."""
    if reg.subscription_id:
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
    amount = CAMPAIGN_REVIEW_CENTS + UPFRONT_MONTHS * MONTHLY_CENTS[reg.fee_tier]
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
    return (
        f"The carrier did not accept this business profile. ${amount / 100:.2f} was refunded; "
        f"${(BRAND_FEE_CENTS + SERVICE_FEE_CENTS) / 100:.2f} for the registration filing is "
        "not refundable."
    )
