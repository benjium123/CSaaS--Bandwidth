"""P41 telephony gate: is this org allowed to text, call and buy numbers at all?

Sits BESIDE the prepaid credit gate (services/telephony_billing.py), never inside it: the
credit gate returns early for orgs that are not prepaid, and verification must apply to
every org. Every outbound path calls ``require_telephony_allowed`` before the credit gate:

  texting   services/messaging.py  send_message (raise) + _dispatch_to_carrier (as data)
  calling   services/calls.py      create_outbound_call, start_blind_transfer
            voice_plane/service.py start_room_call, transfer_room_call
  numbers   routes/numbers.py      add_number, order
            services/telephony_provisioning.py  provision, order_number

Inbound traffic is never refused here - a suspended business's customers can still reach
it until an operator releases the numbers.

With KYC_ENFORCED off nothing in the verification block is checked (development and
pre-P41 tests).

Individual workspaces (Org.account_type == "individual") have one extra rule that runs at
the very top of refusal(), before the monitor and before verification: they never text
(kind "sms"/"sms_dispatch" is refused outright), and calling or buying numbers always
requires an approved KYC profile with a recorded decision (decided_by/decided_at) even
when KYC_ENFORCED is off. Business workspaces are untouched by any of this.

A SECOND, independent requirement sits beside verification: an entitled Stripe
subscription (models/subscriptions.is_entitled). It is behind
REQUIRE_SUBSCRIPTION_FOR_TELEPHONY, which defaults to FALSE - with it off the
subscription is not even queried and this gate behaves exactly as it did before
subscriptions existed. Its refusal code is "subscription_required", deliberately
distinct from "account_not_verified" so the console can tell "verify your business"
apart from "choose a plan".
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timezone
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_active_settings
from app.db.base import set_org_context
from app.errors import AccountNotVerifiedError, AccountSuspendedError, PermissionDeniedError
from app.models import KYC_TELEPHONY_STATUSES, Call, KycProfile, Message, Org, OrgNumber

#: "sms_dispatch" = the send-time re-check of an already-created message: status and
#: deposit only, because the daily counter already includes that message.
Kind = Literal["sms", "sms_dispatch", "call", "number"]

#: messaging failure codes written when a queued message is refused at dispatch.
REFUSAL_PUBLIC_TEXT = {
    "account_not_verified": "Not sent - texting unlocks once your business is verified.",
    "account_suspended": "Not sent - this account is suspended.",
    "daily_limit_reached": "Not sent - today's texting limit for this account was reached.",
    "account_paused": "Not sent - calling and texting are paused while we review this account.",
    "subscription_required": "Not sent - choose a plan to start texting.",
    "individual_messaging_disabled": "Not sent - texting is not available for individual accounts.",
}

#: account types an org may declare. Anything else (including NULL) is treated as business.
_INDIVIDUAL_ACCOUNT_TYPE = "individual"


def _settings_of(session: AsyncSession) -> Settings:
    bound = session.info.get("settings")
    return bound if isinstance(bound, Settings) else get_active_settings()


async def _org(session: AsyncSession, org_id: uuid.UUID) -> Org | None:
    # Org is deliberately NOT TenantScoped - it is the tenant itself - so it is fetched by
    # primary key without an org-context query filter.
    return (await session.execute(sa.select(Org).where(Org.id == org_id))).scalar_one_or_none()


async def _profile(session: AsyncSession, org_id: uuid.UUID) -> KycProfile | None:
    set_org_context(session, org_id)
    return (
        await session.execute(sa.select(KycProfile).where(KycProfile.org_id == org_id))
    ).scalar_one_or_none()


def _day_start(now: datetime) -> datetime:
    return datetime.combine(now.date(), time.min, tzinfo=timezone.utc)


async def refusal(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    kind: Kind,
    *,
    now: datetime | None = None,
) -> str | None:
    """None when allowed, else a machine code: account_not_verified, account_suspended,
    account_paused, daily_limit_reached, number_limit_reached, deposit_required,
    subscription_required, individual_messaging_disabled."""
    # Individual workspaces are handled first, before the monitor and before verification:
    # an individual org can never text (kind "sms"/"sms_dispatch"), and for calls and
    # numbers it must pass KYC below regardless of KYC_ENFORCED (see kyc_required). A
    # business org (or an org row we could not find) skips this entirely.
    org = await _org(session, org_id)
    is_individual = org is not None and org.account_type == _INDIVIDUAL_ACCOUNT_TYPE
    if is_individual and kind in ("sms", "sms_dispatch"):
        return "individual_messaging_disabled"

    # P43: the traffic monitor's automatic pause / restriction applies whether or not
    # business verification is enforced.
    from app.services import monitor_score

    monitored = await monitor_score.refusal(session, settings, org_id, kind)
    if monitored is not None:
        return monitored
    # Business verification. Unchanged for every business org: the block below is what it
    # always was, nested under the flag it was already guarded by, so the subscription
    # check can run after it rather than being skipped by its early return. Individual
    # orgs additionally always reach it for calling and numbers.
    # Org.kyc_required lets an operator require verification on a single workspace;
    # getattr keeps that safe when the org row is absent (None) or predates the column.
    kyc_required = (
        settings.kyc_enforced
        or bool(getattr(org, "kyc_required", False))
        or (is_individual and kind in ("call", "number"))
    )
    if kyc_required:
        profile = await _profile(session, org_id)
        if profile is None:
            return "account_not_verified"
        if profile.status == "suspended":
            return "account_suspended"
        if profile.status not in KYC_TELEPHONY_STATUSES:
            return "account_not_verified"
        if is_individual and (profile.decided_by is None or profile.decided_at is None):
            # A stored status is not enough for an individual: it must carry the operator
            # decision that approved it.
            return "account_not_verified"

        if profile.deposit_required_cents:
            from app.services import credits

            if await credits.balance(session, org_id) < profile.deposit_required_cents * 10_000:
                return "deposit_required"

        limits = profile.limits or {}
        now = now or datetime.now(timezone.utc)
        if kind == "sms" and limits.get("daily_texts") is not None:
            sent = (
                await session.execute(
                    sa.select(sa.func.count(Message.id)).where(
                        Message.org_id == org_id,
                        Message.direction == "outbound",
                        Message.created_at >= _day_start(now),
                    )
                )
            ).scalar_one()
            if sent >= limits["daily_texts"]:
                return "daily_limit_reached"
        if kind == "call" and limits.get("daily_calls") is not None:
            placed = (
                await session.execute(
                    sa.select(sa.func.count(Call.id)).where(
                        Call.org_id == org_id,
                        Call.direction == "outbound",
                        Call.created_at >= _day_start(now),
                    )
                )
            ).scalar_one()
            if placed >= limits["daily_calls"]:
                return "daily_limit_reached"
        if kind == "number" and limits.get("max_numbers") is not None:
            held = (
                await session.execute(
                    sa.select(sa.func.count(OrgNumber.id)).where(OrgNumber.org_id == org_id)
                )
            ).scalar_one()
            if held >= limits["max_numbers"]:
                return "number_limit_reached"

    # Second, INDEPENDENT requirement, and the flag defaults to False: with it off the
    # subscription is never queried, so an org that has never heard of a plan is refused
    # nothing it was allowed yesterday. Verification keeps precedence above - an
    # unverified business is told to verify, not to go and pick a plan it cannot use.
    billing_org = await _org(session, org_id)
    if settings.require_subscription_for_telephony or (
        billing_org and billing_org.number_subscription_required and kind != "number"
    ):
        from app.services import subscriptions as subscriptions_svc

        if not await subscriptions_svc.has_entitled_subscription(session, org_id):
            return "subscription_required"
    return None


def _raise_for(code: str) -> None:
    if code == "account_suspended":
        raise AccountSuspendedError()
    if code == "account_not_verified":
        raise AccountNotVerifiedError()
    if code == "deposit_required":
        raise PermissionDeniedError(
            "Add the required deposit to your balance to start calling and texting",
            code="deposit_required",
        )
    if code == "daily_limit_reached":
        raise PermissionDeniedError(
            "This account reached today's limit. It resets at midnight UTC, or ask support "
            "to raise it.",
            code="daily_limit_reached",
        )
    if code == "account_paused":
        raise PermissionDeniedError(
            "Calling and texting are paused while our team reviews recent activity on this "
            "account. You can send us an explanation from the console.",
            code="account_paused",
        )
    if code == "subscription_required":
        # Its own code, never AccountNotVerifiedError: "verify your business" and "choose a
        # plan" are different actions and the console must not conflate them.
        raise PermissionDeniedError(
            "Choose a plan to start calling and texting", code="subscription_required"
        )
    if code == "individual_messaging_disabled":
        # Individual workspaces cannot text at all; this is not a verification problem, so
        # it gets its own code rather than AccountNotVerifiedError.
        raise PermissionDeniedError(
            "Texting is not available for individual accounts",
            code="individual_messaging_disabled",
        )
    if code == "number_limit_reached":
        raise PermissionDeniedError(
            "This account holds the most numbers it is allowed. Ask support to raise the limit.",
            code="number_limit_reached",
        )
    raise PermissionDeniedError("Telephony is not available for this account", code=code)


async def require_telephony_allowed(
    session: AsyncSession, org_id: uuid.UUID, kind: Kind, *, settings: Settings | None = None
) -> None:
    settings = settings or _settings_of(session)
    code = await refusal(session, settings, org_id, kind)
    if code is not None:
        _raise_for(code)


async def telephony_allowed(
    session: AsyncSession, org_id: uuid.UUID, kind: Kind, *, settings: Settings | None = None
) -> str | None:
    """Dispatch-time variant: returns the refusal code (None = allowed), never raises."""
    settings = settings or _settings_of(session)
    return await refusal(session, settings, org_id, kind)
