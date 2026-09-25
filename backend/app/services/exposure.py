"""P44b: how much damage one workspace can do in a day, whatever it is allowed to reach.

The destination firewall (destination_policy.py) decides WHERE traffic may go; this module
bounds HOW MUCH of it there can be, because the prepaid hard stop only bounds the balance
and auto-recharge refills the balance - a stolen card would keep paying.

  concurrent calls   live calls per phone number (2) and outbound calls per person (2); a
                     workspace-wide cap only when an operator sets one
  daily spend        usage debited to the ledger since UTC midnight; kept LOW on purpose
                     ($10/day for a workspace younger than FRAUD_NEW_ACCOUNT_DAYS, $50/day
                     after that). The carrier-side cap is a high backstop, this is the brake.
  auto-recharge      none at all while the workspace is new; at most N charges a day after

Operators raise a single workspace through ``KycProfile.limits`` (the same JSON the P41
limits already use): ``max_concurrent_calls`` and ``daily_spend_micros``. Everything here
applies to every workspace, verified or not, because it is fraud control, not onboarding.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import ALLOW_UNSCOPED_KEY
from app.models import Call, KycProfile, Org
from app.models.voice import TERMINAL_CALL_STATUSES

CONCURRENT = "concurrent_call_limit"
DAILY_SPEND = "daily_spend_reached"

#: A call row that never reached a terminal status (lost webhook) must not hold a slot
#: forever: only rows this recent count as live.
LIVE_CALL_WINDOW = timedelta(hours=4)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _profile(session: AsyncSession, org_id: uuid.UUID) -> KycProfile | None:
    # JUSTIFIED allow_unscoped: filtered on the org_id the caller is acting for.
    return (
        await session.execute(
            sa.select(KycProfile)
            .where(KycProfile.org_id == org_id)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one_or_none()


async def account_started(session: AsyncSession, org_id: uuid.UUID) -> datetime | None:
    """When the workspace's track record starts: its creation. Not the verification
    decision - an operator re-decision would move that forward and make an established
    account "new" again."""
    org = await session.get(Org, org_id)
    return _aware(org.created_at) if org is not None else None


async def is_new(session: AsyncSession, settings: Settings, org_id: uuid.UUID) -> bool:
    # An operator can vouch for a workspace early (limits.established = true).
    if (await _limits(session, org_id)).get("established") is True:
        return False
    started = await account_started(session, org_id)
    days = int(getattr(settings, "fraud_new_account_days", 30))
    return started is None or _now() - started < timedelta(days=days)


async def _limits(session: AsyncSession, org_id: uuid.UUID) -> dict:
    profile = await _profile(session, org_id)
    return dict(profile.limits or {}) if profile is not None and profile.limits else {}


async def _live_calls(session: AsyncSession, org_id: uuid.UUID) -> list[Call]:
    """The workspace's calls still in progress. 911/933 calls are left out: they never use
    up a slot another call needs."""
    return list(
        (
            await session.execute(
                sa.select(Call)
                .where(
                    Call.org_id == org_id,
                    Call.status.not_in(TERMINAL_CALL_STATUSES),
                    Call.created_at >= _now() - LIVE_CALL_WINDOW,
                    sa.or_(Call.tag.is_(None), Call.tag != "emergency"),
                )
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )


async def live_outbound_calls(session: AsyncSession, org_id: uuid.UUID) -> int:
    return sum(1 for c in await _live_calls(session, org_id) if c.direction == "outbound")


def placed_by(identity: str | None) -> uuid.UUID | None:
    """The person behind a room-call identity (``user-<uuid>``); None for the dialer, the
    assistant test line or anything else that is not a person."""
    if identity and identity.startswith("user-"):
        try:
            return uuid.UUID(identity[5:])
        except ValueError:
            return None
    return None


async def workspace_call_cap(session: AsyncSession, org_id: uuid.UUID) -> int | None:
    """An operator-set cap on the whole workspace, if any (limits.max_concurrent_calls)."""
    value = (await _limits(session, org_id)).get("max_concurrent_calls")
    return value if isinstance(value, int) and value > 0 else None


async def free_call_slots(
    session: AsyncSession, settings: Settings, org_id: uuid.UUID
) -> int:
    """How many more outbound calls the workspace could start now: the free per-number
    slots over its active numbers, bounded by the workspace cap when one is set."""
    from app.models import OrgNumber

    per_number = int(getattr(settings, "fraud_calls_per_number", 2))
    numbers = (
        await session.execute(
            sa.select(OrgNumber.e164)
            .where(OrgNumber.org_id == org_id, OrgNumber.status == "active")
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    live = await _live_calls(session, org_id)
    free = sum(max(per_number - sum(1 for c in live if c.our_e164 == n), 0) for n in numbers)
    cap = await workspace_call_cap(session, org_id)
    if cap is not None:
        free = min(free, cap - sum(1 for c in live if c.direction == "outbound"))
    return max(free, 0)


async def spent_today_micros(session: AsyncSession, org_id: uuid.UUID) -> int:
    from app.models import CreditLedgerEntry

    midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(-CreditLedgerEntry.amount_micros), 0))
            .where(
                CreditLedgerEntry.org_id == org_id,
                CreditLedgerEntry.entry_type == "usage",
                CreditLedgerEntry.created_at >= midnight,
            )
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    return max(int(total), 0)


async def daily_spend_ceiling_micros(
    session: AsyncSession, settings: Settings, org_id: uuid.UUID
) -> int:
    value = (await _limits(session, org_id)).get("daily_spend_micros")
    if isinstance(value, int) and value > 0:
        return value
    if await is_new(session, settings, org_id):
        return int(getattr(settings, "fraud_new_account_daily_spend_micros", 10_000_000))
    return int(getattr(settings, "fraud_daily_spend_micros", 50_000_000))


async def refusal(
    session: AsyncSession, settings: Settings, org_id: uuid.UUID, kind: str
) -> str | None:
    """Telephony gate hook (telephony_access.refusal): the daily spend ceiling."""
    if not getattr(settings, "fraud_exposure_enforced", True):
        return None
    if kind in ("call", "sms", "sms_dispatch"):  # dispatch: queued, held and campaign texts
        if await spent_today_micros(session, org_id) >= await daily_spend_ceiling_micros(
            session, settings, org_id
        ):
            return DAILY_SPEND
    return None


async def require_call_slot(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    *,
    from_e164: str,
    user_id: uuid.UUID | None = None,
) -> None:
    """Refuse a NEW outbound call when its number already carries its maximum of live calls
    (in or out), when the person placing it already has their maximum going, or when the
    workspace hits an operator-set cap.

    Called only where a new Call row is about to be created (carrier dial, room/SIP dial);
    a transfer or a fax continues or is not a live call and never takes a slot. Read then
    insert, so a burst can overshoot by the few calls in flight together - acceptable, the
    carrier-side concurrent limit (deploy/fraud_carrier_limits.py) is the hard backstop.
    """
    if not getattr(settings, "fraud_exposure_enforced", True):
        return
    live = await _live_calls(session, org_id)
    per_number = int(getattr(settings, "fraud_calls_per_number", 2))
    per_user = int(getattr(settings, "fraud_calls_per_user", 2))
    cap = await workspace_call_cap(session, org_id)
    outbound = [c for c in live if c.direction == "outbound"]
    full = sum(1 for c in live if c.our_e164 == from_e164) >= per_number
    if user_id is not None:
        mine = sum(1 for c in outbound if (c.extra or {}).get("placed_by") == str(user_id))
        full = full or mine >= per_user
    if cap is not None:
        full = full or len(outbound) >= cap
    if full:
        from app.services import telephony_access, telephony_billing

        await telephony_billing.record_refusal(session, org_id, kind="call", reason=CONCURRENT)
        telephony_access._raise_for(CONCURRENT)


def recharges_in_last_day(auto: dict, now: datetime | None = None) -> list[dict]:
    """Auto-recharge charges of the last 24 h, from ``credit_auto_recharge.history``
    (entries ``{"at": iso, "micros": int}``), pruned to that window."""
    now = now or _now()
    kept: list[dict] = []
    for entry in auto.get("history") or []:
        if not isinstance(entry, dict):
            continue
        try:
            when = _aware(datetime.fromisoformat(str(entry.get("at"))))
        except ValueError:
            continue
        if when is not None and now - when < timedelta(days=1):
            kept.append({"at": when.isoformat(), "micros": int(entry.get("micros") or 0)})
    return kept


async def auto_recharge_refusal(
    session: AsyncSession, settings: Settings, org, amount_micros: int  # noqa: ANN001
) -> str | None:
    """Why this auto-recharge must not run, or None. A stolen card with auto-recharge on
    would otherwise refill an account being drained by fraud, forever."""
    if not getattr(settings, "fraud_exposure_enforced", True):
        return None
    if await is_new(session, settings, org.id):
        return "new_account"
    recent = recharges_in_last_day(getattr(org, "credit_auto_recharge", None) or {})
    if len(recent) >= int(getattr(settings, "fraud_auto_recharge_max_per_day", 3)):
        return "daily_count"
    cap = int(getattr(settings, "fraud_auto_recharge_max_micros_per_day", 300_000_000))
    if sum(e["micros"] for e in recent) + int(amount_micros) > cap:
        return "daily_amount"
    return None


async def alert_auto_recharge_cap(session: AsyncSession, org_id: uuid.UUID, reason: str) -> None:
    """One open operator alert per workspace per UTC day."""
    from app.models import SecurityAlert

    midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    exists = (
        await session.execute(
            sa.select(SecurityAlert.id)
            .where(
                SecurityAlert.org_id == org_id,
                SecurityAlert.kind == "auto_recharge_cap",
                SecurityAlert.created_at >= midnight,
            )
            .limit(1)
        )
    ).first()
    if exists is None:
        session.add(
            SecurityAlert(
                id=uuid.uuid4(),
                kind="auto_recharge_cap",
                org_id=org_id,
                detail={"reason": reason},
            )
        )
