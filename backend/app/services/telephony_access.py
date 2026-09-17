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

With KYC_ENFORCED off nothing is checked (development and pre-P41 tests).
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
from app.models import KYC_TELEPHONY_STATUSES, Call, KycProfile, Message, OrgNumber

#: "sms_dispatch" = the send-time re-check of an already-created message: status and
#: deposit only, because the daily counter already includes that message.
Kind = Literal["sms", "sms_dispatch", "call", "number"]

#: messaging failure codes written when a queued message is refused at dispatch.
REFUSAL_PUBLIC_TEXT = {
    "account_not_verified": "Not sent - texting unlocks once your business is verified.",
    "account_suspended": "Not sent - this account is suspended.",
    "daily_limit_reached": "Not sent - today's texting limit for this account was reached.",
    "account_paused": "Not sent - calling and texting are paused while we review this account.",
}


def _settings_of(session: AsyncSession) -> Settings:
    bound = session.info.get("settings")
    return bound if isinstance(bound, Settings) else get_active_settings()


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
    account_paused, daily_limit_reached, number_limit_reached, deposit_required."""
    # P43: the traffic monitor's automatic pause / restriction applies whether or not
    # business verification is enforced.
    from app.services import monitor_score

    monitored = await monitor_score.refusal(session, settings, org_id, kind)
    if monitored is not None:
        return monitored
    if not settings.kyc_enforced:
        return None
    profile = await _profile(session, org_id)
    if profile is None:
        return "account_not_verified"
    if profile.status == "suspended":
        return "account_suspended"
    if profile.status not in KYC_TELEPHONY_STATUSES:
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
