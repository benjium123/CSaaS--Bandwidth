"""Billing v2 low-balance alerts and org billing state (money-owned).

- warn threshold = max($5, average daily spend over the last 7 days), refreshed hourly.
- billing_state: ``exhausted`` at or below $0, ``low`` below the threshold, else ``ok``.
- Crossing into low/exhausted alerts every owner/admin ONCE per level per top-up cycle:
  a bell notification (kind ``low_balance``, which the app turns into a popup) and an email.
  A top-up re-arms it (the dedupe key carries the last top-up reference).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import CreditLedgerEntry, Org, OrgMembership, Role, User
from app.services import credits

log = structlog.get_logger("billing_alerts")

MIN_WARN_THRESHOLD_MICROS = 5_000_000
AVG_WINDOW_DAYS = 7
ALERT_ROLES = ("owner", "admin")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def state_for(balance_micros: int, threshold_micros: int) -> str:
    if balance_micros <= 0:
        return "exhausted"
    if balance_micros < max(threshold_micros, MIN_WARN_THRESHOLD_MICROS):
        return "low"
    return "ok"


async def avg_daily_spend(
    session: AsyncSession, org_id: uuid.UUID, *, now: datetime | None = None
) -> int:
    """Usage debits over the last AVG_WINDOW_DAYS days / AVG_WINDOW_DAYS, in micros."""
    since = (now or _now()) - timedelta(days=AVG_WINDOW_DAYS)
    set_org_context(session, org_id)
    total = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(-CreditLedgerEntry.amount_micros), 0)).where(
                CreditLedgerEntry.entry_type == "usage",
                CreditLedgerEntry.created_at >= since,
            )
        )
    ).scalar_one()
    return max(int(total), 0) // AVG_WINDOW_DAYS


async def refresh_thresholds(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Hourly: recompute avg spend + warn threshold for every prepaid org. One commit per
    org."""
    org_ids = (
        await session.execute(
            sa.select(Org.id).where(Org.telephony_prepaid.is_(True))
        )
    ).scalars().all()
    done = 0
    for org_id in org_ids:
        try:
            avg = await avg_daily_spend(session, org_id, now=now)
            org = await session.get(Org, org_id)
            if org is None:
                continue
            org.avg_daily_spend_micros = avg
            org.warn_threshold_micros = max(MIN_WARN_THRESHOLD_MICROS, avg)
            await session.commit()
            done += 1
        except Exception:
            await session.rollback()
            log.exception("billing_alerts.refresh_failed", org_id=str(org_id))
    return done


async def _recipients(session: AsyncSession, org_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    rows = (
        await session.execute(
            sa.select(User.id, User.email)
            .join(OrgMembership, OrgMembership.user_id == User.id)
            .join(Role, Role.id == OrgMembership.role_id)
            .where(OrgMembership.org_id == org_id, Role.name.in_(ALERT_ROLES))
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).all()
    seen: dict[uuid.UUID, str] = {}
    for uid, email in rows:
        seen[uid] = email or ""
    return list(seen.items())


async def _last_topup_ref(session: AsyncSession, org_id: uuid.UUID) -> str:
    set_org_context(session, org_id)
    ref = (
        await session.execute(
            sa.select(CreditLedgerEntry.reference)
            .where(CreditLedgerEntry.entry_type == "topup")
            .order_by(CreditLedgerEntry.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return str(ref or "none")


async def _has_call_minutes(session: AsyncSession, org_id) -> bool:  # noqa: ANN001
    from app.services import bundles

    return await bundles.units(session, org_id, "voice") > 0


def _money(micros: int) -> str:
    return f"${micros / 1_000_000:,.2f}"


async def evaluate(session: AsyncSession, settings, org: Org) -> str:  # noqa: ANN001
    """Set org.billing_state and send the crossing alert if due. Does not commit.
    Returns the state. Orgs that are not prepaid are always ``ok`` and never alerted."""
    if not org.telephony_prepaid:
        if org.billing_state != "ok":
            org.billing_state = "ok"
            org.billing_state_changed_at = _now()
        return "ok"
    # Holds are ledger debits that come back when a call ends; judging the level on the
    # balance alone would flip low <-> exhausted around every call.
    balance = await credits.balance(session, org.id) + await credits.outstanding_reserves(
        session, org.id
    )
    state = state_for(balance, int(org.warn_threshold_micros or 0))
    if state == "exhausted" and await _has_call_minutes(session, org.id):
        # $0 but call-minute bundle minutes left: calls keep working (bundles are spent
        # before the balance), so this is "low" - never tell them calls are declined.
        # SMS/MMS units alone do not count: calls really are paused then.
        state = "low"
    if state != org.billing_state:
        org.billing_state = state
        org.billing_state_changed_at = _now()
    if state == "ok":
        return state

    ref = await _last_topup_ref(session, org.id)
    key = f"lowbal:{ref}:{state}"[:128]
    prev = org.low_balance_alert_key or ""
    rank = {"low": 1, "exhausted": 2}
    prev_ref, _, prev_state = prev[len("lowbal:"):].rpartition(":") if prev else ("", "", "")
    # One alert per level per top-up cycle, and only when things got WORSE.
    if prev_ref == ref and rank.get(prev_state, 0) >= rank[state]:
        return state
    org.low_balance_alert_key = key
    # Commit the key BEFORE sending, so a failure later in the tick cannot resend it.
    await session.commit()
    set_org_context(session, org.id)

    from app.services import mailer, notifications

    if state == "exhausted":
        subject = "Your balance is empty - calling and texting are paused"
        body = (
            f"Your {settings.app_name} balance is {_money(balance)}. Outgoing texts, calls "
            "and faxes are paused and incoming calls are being declined until you add "
            "credit or buy a bundle."
        )
    else:
        subject = "Your balance is running low"
        body = (
            f"Your {settings.app_name} balance is {_money(balance)}, below your warning "
            f"level of {_money(max(int(org.warn_threshold_micros or 0), MIN_WARN_THRESHOLD_MICROS))} "
            "(about a day of your usage). Add credit or turn on auto-recharge so your "
            "numbers keep working."
        )
    base = (getattr(settings, "public_web_url", "") or "").rstrip("/")
    link = f"\n\nAdd credit: {base}/settings/billing" if base else ""
    recipients = await _recipients(session, org.id)
    set_org_context(session, org.id)
    for user_id, _email in recipients:
        try:
            await notifications.create(
                session,
                org.id,
                user_id=user_id,
                kind="low_balance",
                body=subject,
                dedupe_key=key,
            )
        except Exception:
            log.exception("billing_alerts.notification_failed", org_id=str(org.id))
    emails = [e for _u, e in recipients if e]
    if emails:
        try:
            await mailer.send(settings, emails, f"{settings.app_name}: {subject}", body + link)
        except Exception:
            log.exception("billing_alerts.email_failed", org_id=str(org.id))
    log.info("billing_alerts.sent", org_id=str(org.id), state=state, recipients=len(recipients))
    return state


async def notify_owners(session: AsyncSession, settings, org: Org, subject: str, body: str,  # noqa: ANN001
                        *, dedupe_key: str) -> None:
    """One-off billing notice (auto-recharge declined / disabled). Does not commit."""
    from app.services import mailer, notifications

    recipients = await _recipients(session, org.id)
    set_org_context(session, org.id)
    for user_id, _email in recipients:
        try:
            await notifications.create(
                session, org.id, user_id=user_id, kind="low_balance", body=subject,
                dedupe_key=dedupe_key[:128],
            )
        except Exception:
            log.exception("billing_alerts.notification_failed", org_id=str(org.id))
    emails = [e for _u, e in recipients if e]
    if emails:
        try:
            await mailer.send(settings, emails, f"{settings.app_name}: {subject}", body)
        except Exception:
            log.exception("billing_alerts.email_failed", org_id=str(org.id))
