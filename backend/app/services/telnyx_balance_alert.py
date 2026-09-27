"""Telnyx balance alert: every active ops admin is emailed once when the carrier balance
(available credit, which is what stops calls and texts) drops below the floor, and again
only after it has recovered above the floor and dropped again.

Checked from the sweeper every 15 minutes. The "already alerted" flag lives in the
process, so a restart while the balance is still low sends one reminder.
"""

from __future__ import annotations

import time

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings

log = structlog.get_logger("telnyx_balance_alert")

#: Alert below this many cents ($25).
FLOOR_CENTS = 2_500
CHECK_INTERVAL_SECONDS = 900

_last_run: float | None = None
_alerted = False


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


async def tick(session: AsyncSession, settings: Settings, *, fetch=None) -> dict[str, int]:  # noqa: ANN001
    global _last_run, _alerted
    mono = time.monotonic()
    if _last_run is not None and mono - _last_run < CHECK_INTERVAL_SECONDS:
        return {}
    _last_run = mono
    found = await (fetch or fetch_balance_cents)(settings)
    if found is None:
        return {}
    available, balance = found
    if available >= FLOOR_CENTS:
        _alerted = False
        return {"telnyx_available_cents": available}
    if _alerted:
        return {"telnyx_available_cents": available}

    from app.services import break_glass, mailer

    to = await break_glass.admin_emails(session)
    log.error("telnyx_balance_low", available_cents=available, balance_cents=balance)
    if to:
        await mailer.send(
            settings,
            to,
            f"Telnyx balance low on {settings.app_name}: ${available / 100:,.2f}",
            (
                f"The Telnyx account has ${available / 100:,.2f} available "
                f"(balance ${balance / 100:,.2f}), under the ${FLOOR_CENTS // 100} alert level.\n\n"
                "When it reaches $0, calls, texts and number orders for every workspace stop. "
                "Add funds in the Telnyx portal (Billing), or turn on Telnyx auto-recharge.\n\n"
                "You will not get this email again until the balance recovers above "
                f"${FLOOR_CENTS // 100} and drops again."
            ),
        )
    _alerted = True
    return {"telnyx_available_cents": available, "telnyx_balance_alerted": 1}
