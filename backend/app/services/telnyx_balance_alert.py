"""Telnyx balance alerts to every active ops admin. Checked from the sweeper every 15 minutes
on the Telnyx *available credit* (what actually stops calls and texts).

Rules (user-approved 2026-09-28):
- under $25 -> one warning; under $10 -> one urgent email right away (even after the
  warning); $0 or less -> one "service stopped" email. At most one email per level per drop.
- while still under $25: at most one reminder every 24 hours, at the current level.
- back at $25 or more after an alert -> one "back to normal" email; every level re-arms.
The state (level + when the last email went) is kept in ``platform_settings`` so a deploy or
restart never sends it again. A failed balance lookup never alerts.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings

log = structlog.get_logger("telnyx_balance_alert")

WARN_CENTS = 2_500  # $25
URGENT_CENTS = 1_000  # $10
REMIND_EVERY = timedelta(hours=24)
CHECK_INTERVAL_SECONDS = 900
STATE_KEY = "telnyx_balance_alert"

#: Severity order; "ok" is not an alert.
LEVELS = ("ok", "warn", "urgent", "stopped")

_last_run: float | None = None


def level_for(available_cents: int) -> str:
    if available_cents <= 0:
        return "stopped"
    if available_cents < URGENT_CENTS:
        return "urgent"
    if available_cents < WARN_CENTS:
        return "warn"
    return "ok"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def fetch_balance_cents(settings: Settings) -> tuple[int, int] | None:
    """(available credit, balance) in cents; None when Telnyx is not configured or the
    lookup failed (a failed lookup must never look like an empty balance)."""
    import httpx

    key = settings.telnyx_api_key.get_secret_value()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.telnyx.com/v2/balance", headers={"Authorization": f"Bearer {key}"}
            )
            resp.raise_for_status()
            data = resp.json()["data"]
        balance = round(float(data.get("balance") or 0) * 100)
        available = data.get("available_credit")
        available = round(float(available) * 100) if available is not None else balance
        return available, balance
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        log.warning("telnyx_balance_lookup_failed")
        return None


def _message(kind: str, level: str, available: int, balance: int, app_name: str) -> tuple[str, str]:
    money = f"${available / 100:,.2f}"
    detail = f"The Telnyx account has {money} available (balance ${balance / 100:,.2f})."
    if kind == "recovered":
        return (
            f"Telnyx balance back to normal on {app_name}: {money}",
            f"{detail}\n\nIt is back above ${WARN_CENTS // 100}. Alerts are re-armed.",
        )
    title = {
        "warn": f"Telnyx balance low on {app_name}: {money}",
        "urgent": f"URGENT: Telnyx balance under ${URGENT_CENTS // 100} on {app_name}: {money}",
        "stopped": f"Telnyx balance is empty on {app_name}: calls and texts are stopping",
    }[level]
    if kind == "reminder":
        title = f"Reminder: {title}"
    return title, (
        f"{detail}\n\n"
        "When it reaches $0, calls, texts and number orders for every workspace stop. "
        "Add funds in the Telnyx portal (Billing), or turn on Telnyx auto-recharge.\n\n"
        f"Next: at most one reminder a day while it stays under ${WARN_CENTS // 100}, one urgent "
        f"email under ${URGENT_CENTS // 100}, one when it is empty, and one when it recovers."
    )


async def tick(session: AsyncSession, settings: Settings, *, fetch=None) -> dict[str, int]:  # noqa: ANN001
    global _last_run
    mono = time.monotonic()
    if _last_run is not None and mono - _last_run < CHECK_INTERVAL_SECONDS:
        return {}
    _last_run = mono
    found = await (fetch or fetch_balance_cents)(settings)
    if found is None:
        return {}
    available, balance = found

    from app.models import PlatformSetting

    row = await session.get(PlatformSetting, STATE_KEY)
    state = dict((row.value if row is not None else None) or {})
    prev = state.get("level") if state.get("level") in LEVELS else "ok"
    last_sent = state.get("last_sent_at")
    last_sent_at = datetime.fromisoformat(last_sent) if isinstance(last_sent, str) else None
    level = level_for(available)
    now = _now()

    kind: str | None = None
    if level == "ok":
        if prev != "ok":
            kind = "recovered"
    elif LEVELS.index(level) > LEVELS.index(prev):
        kind = "alert"
    elif last_sent_at is None or now - last_sent_at >= REMIND_EVERY:
        kind = "reminder"

    out = {"telnyx_available_cents": available}
    if kind is not None:
        from app.services import break_glass, mailer

        to = await break_glass.admin_emails(session)
        log.error(
            "telnyx_balance_low" if level != "ok" else "telnyx_balance_recovered",
            available_cents=available, balance_cents=balance, level=level, kind=kind,
        )
        if to:
            subject, body = _message(kind, level, available, balance, settings.app_name)
            await mailer.send(settings, to, subject, body)
        state["last_sent_at"] = now.isoformat()
        out["telnyx_balance_alerted"] = 1
    if kind is not None or level != prev:
        state["level"] = level
        if row is None:
            session.add(PlatformSetting(key=STATE_KEY, value=state))
        else:
            row.value = state
        await session.commit()
    return out
