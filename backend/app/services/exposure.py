"""P44b: how much damage one workspace can do in a day, whatever it is allowed to reach.

The destination firewall (destination_policy.py) decides WHERE traffic may go; this module
bounds HOW MUCH of it there can be, because the prepaid hard stop only bounds the balance
and auto-recharge refills the balance - a stolen card would keep paying.

  concurrent calls   outbound calls not yet finished, per workspace (default 5)
  daily spend        usage debited to the ledger since UTC midnight; $25/day for a workspace
                     younger than FRAUD_NEW_ACCOUNT_DAYS, $250/day after that
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


async def live_outbound_calls(session: AsyncSession, org_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                sa.select(sa.func.count(Call.id))
                .where(
                    Call.org_id == org_id,
                    Call.direction == "outbound",
                    Call.status.not_in(TERMINAL_CALL_STATUSES),
                    Call.created_at >= _now() - LIVE_CALL_WINDOW,
                    # A 911/933 call never uses up a slot another call needs.
                    sa.or_(Call.tag.is_(None), Call.tag != "emergency"),
                )
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one()
    )


async def max_concurrent_calls(
    session: AsyncSession, settings: Settings, org_id: uuid.UUID
) -> int:
    value = (await _limits(session, org_id)).get("max_concurrent_calls")
    if isinstance(value, int) and value > 0:
        return value
    return int(getattr(settings, "fraud_default_concurrent_calls", 5))


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
        return int(getattr(settings, "fraud_new_account_daily_spend_micros", 25_000_000))
    return int(getattr(settings, "fraud_daily_spend_micros", 250_000_000))


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
    session: AsyncSession, settings: Settings, org_id: uuid.UUID
) -> None:
    """Refuse a NEW outbound call when the workspace already runs its maximum at once.

    Called only where a new Call row is about to be created (carrier dial, room/SIP dial);
    a transfer or a fax continues or is not a live call and never takes a slot. Read then
    insert, so a burst can overshoot by the few calls in flight together - acceptable, the
    carrier-side concurrent limit (deploy/fraud_carrier_limits.py) is the hard backstop.
    """
    if not getattr(settings, "fraud_exposure_enforced", True):
        return
    if await live_outbound_calls(session, org_id) >= await max_concurrent_calls(
        session, settings, org_id
    ):
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
