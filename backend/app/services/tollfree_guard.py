"""P44g: toll-free inbound protection (traffic pumping).

On a toll-free number the CALLED party pays for every inbound minute. Pumping schemes
robo-dial toll-free numbers to generate those minutes (and share the revenue on the
originating side). A workspace's own balance - and, below zero, ours - pays for it.

Checks, applied when an inbound call to a toll-free number is created (before it rings):
  tf_bad_caller    no caller ID, an unparseable one, or a caller outside the North American
                   numbering plan / from the Caribbean IRSF ranges
  tf_concurrency   more than TOLLFREE_MAX_CONCURRENT live inbound calls on this number
  tf_caller_rate   the same caller rang this workspace's toll-free numbers 3+ times in
                   10 minutes, or 10+ times in an hour
  tf_daily_cap     the workspace already took TOLLFREE_DAILY_MINUTES inbound toll-free
                   minutes today (UTC). Owners are warned at 80% and when the cap is hit.
Local numbers are not affected.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import phonenumbers
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import Call, OrgNumber
from app.models.voice import TERMINAL_CALL_STATUSES

log = structlog.get_logger("tollfree_guard")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def bad_caller(contact_e164: str | None) -> bool:
    from app.services.destination_policy import IRSF_NANP_REGIONS

    raw = (contact_e164 or "").strip()
    if not raw.startswith("+"):
        return True
    try:
        number = phonenumbers.parse(raw, None)
    except phonenumbers.NumberParseException:
        return True
    if number.country_code != 1:
        return True
    return phonenumbers.region_code_for_number(number) in IRSF_NANP_REGIONS


async def _count(session: AsyncSession, *where) -> int:  # noqa: ANN002
    return int(
        (
            await session.execute(
                sa.select(sa.func.count(Call.id))
                .where(*where)
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalar_one()
    )


async def minutes_today(session: AsyncSession, org_id: uuid.UUID, numbers: list[str]) -> int:
    midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    seconds = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(Call.duration_seconds), 0))
            .where(
                Call.org_id == org_id,
                Call.direction == "inbound",
                Call.our_e164.in_(numbers),
                Call.created_at >= midnight,
            )
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalar_one()
    return int(seconds or 0) // 60


async def refusal(session: AsyncSession, settings, call: Call) -> str | None:  # noqa: ANN001
    """A refusal code for this NEW inbound call, or None. Never raises."""
    if not getattr(settings, "tollfree_guard_enforced", True):
        return None
    number = (
        await session.execute(
            sa.select(OrgNumber).where(
                OrgNumber.org_id == call.org_id, OrgNumber.e164 == call.our_e164
            )
        )
    ).scalar_one_or_none()
    if number is None or number.number_type != "tollfree":
        return None
    if bad_caller(call.contact_e164):
        return "tf_bad_caller"
    now = _now()
    live = await _count(
        session,
        Call.org_id == call.org_id,
        Call.direction == "inbound",
        Call.our_e164 == call.our_e164,
        Call.id != call.id,
        Call.status.not_in(TERMINAL_CALL_STATUSES),
        Call.created_at >= now - timedelta(hours=4),
    )
    if live >= int(getattr(settings, "tollfree_max_concurrent", 5)):
        return "tf_concurrency"
    tollfree = (
        await session.execute(
            sa.select(OrgNumber.e164).where(
                OrgNumber.org_id == call.org_id, OrgNumber.number_type == "tollfree"
            )
        )
    ).scalars().all()
    same_caller = [
        Call.org_id == call.org_id,
        Call.direction == "inbound",
        Call.contact_e164 == call.contact_e164,
        Call.our_e164.in_(tollfree),
        Call.id != call.id,
    ]
    if await _count(session, *same_caller, Call.created_at >= now - timedelta(minutes=10)) >= 3:
        return "tf_caller_rate"
    if await _count(session, *same_caller, Call.created_at >= now - timedelta(hours=1)) >= 10:
        return "tf_caller_rate"
    cap = int(getattr(settings, "tollfree_daily_minutes", 500))
    used = await minutes_today(session, call.org_id, list(tollfree))
    if used >= int(cap * 0.8):
        await _warn(session, settings, call.org_id, used, cap)
    if used >= cap:
        return "tf_daily_cap"
    return None


async def _warn(session: AsyncSession, settings, org_id: uuid.UUID, used: int, cap: int) -> None:  # noqa: ANN001
    from app.models import Org
    from app.services import billing_alerts

    level = "cap" if used >= cap else "80"
    day = _now().date().isoformat()
    try:
        org = await session.get(Org, org_id)
        if org is None:
            return
        await billing_alerts.notify_owners(
            session,
            settings,
            org,
            (
                f"Toll-free calls stopped: {used} of {cap} inbound minutes used today"
                if level == "cap"
                else f"Toll-free usage high: {used} of {cap} inbound minutes used today"
            ),
            "Inbound toll-free minutes are charged to your account. Unusual volume can be a "
            "traffic-pumping attack. Contact support to raise the daily limit if this is real "
            "traffic.",
            dedupe_key=f"tfcap:{org_id}:{day}:{level}",
        )
        set_org_context(session, org_id)
    except Exception:  # noqa: BLE001 - a warning must never block call handling
        log.exception("tollfree_guard.warn_failed", org_id=str(org_id))


async def refuse(session: AsyncSession, call: Call, code: str) -> None:
    """Record and mark the refusal. Does not commit."""
    from app.services import telephony_billing

    await telephony_billing.record_refusal(
        session, call.org_id, kind="inbound_call", reason=code, detail=call.contact_e164
    )
    set_org_context(session, call.org_id)
    call.extra = {**(call.extra or {}), "refused": code}
