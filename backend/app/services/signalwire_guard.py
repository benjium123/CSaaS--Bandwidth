"""SignalWire balance guard (owner order 2026-10-16): SignalWire is our most expensive
carrier, so its prepaid balance is never allowed to run dry under traffic.

SignalWire has no balance API (checked live: /Accounts/{sid}.json has no balance field and
every balance path is a 404). So an operator enters the balance shown in the SignalWire
dashboard (the "anchor"), and every 5 minutes the sweeper subtracts what SignalWire
actually charged since then:
- voice: /api/voice/logs ``charge`` (PSTN minutes AND the SIP audio leg of every call),
- messaging: /api/messaging/logs ``charge``,
- numbers: one monthly rental per number on each renewal day (date_created's day) that
  fell after the anchor (no API reports rentals; NUMBER_MONTHLY_MICROS is the list price).
These logs are project-wide, so the CRM's SignalWire traffic counts too.

Levels:
- estimate <= $6.00 -> one email ("about to run out, moving to Telnyx numbers").
- estimate <= $5.25 -> SignalWire is PAUSED: outbound calls (never 911/933), outbound texts
  and inbound calls on SignalWire numbers are refused, and one more email goes out.
Entering a new balance (after a top-up) re-arms both levels and lifts the pause when the
new balance is above $5.25. A failed lookup never changes anything.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings

log = structlog.get_logger("signalwire_guard")

STATE_KEY = "signalwire_balance_guard"
WARN_MICROS = 6_000_000  # $6.00
STOP_MICROS = 5_250_000  # $5.25
NUMBER_MONTHLY_MICROS = 1_000_000  # SignalWire local number, $1.00 a month
CHECK_INTERVAL_SECONDS = 300
PAUSE_CACHE_SECONDS = 20
MAX_PAGES = 40
#: The owner asked for the alerts at this address; ops admins get them as well.
OWNER_EMAIL = "benethan011@gmail.com"

_last_run: float | None = None
_pause_cache: tuple[float, bool] | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:  # Compatibility API style: "Sat, 19 Sep 2026 01:30:41 +0000"
            ts = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %z")
        except ValueError:
            return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def renewals_between(created: datetime, start: datetime, end: datetime) -> int:
    """How many monthly renewal days of a number bought at ``created`` fall in (start, end]."""
    count = 0
    month = start.year * 12 + start.month - 1
    while True:
        y, m = divmod(month, 12)
        m += 1
        # A number bought on the 31st renews on the month's last day.
        day = created.day
        while True:
            try:
                renewal = created.replace(year=y, month=m, day=day)
                break
            except ValueError:
                day -= 1
        if renewal > end:
            return count
        if renewal > start and renewal > created:
            count += 1
        month += 1


async def _paged_charges(client, url: str, params: dict, since: datetime) -> int:  # noqa: ANN001
    total = 0
    next_url: str | None = url
    pages = 0
    while next_url and pages < MAX_PAGES:
        resp = await client.get(next_url, params=params if pages == 0 else None)
        resp.raise_for_status()
        body = resp.json()
        for row in body.get("data") or []:
            created = _parse_ts(row.get("created_at"))
            if created is not None and created < since:
                continue
            total += round(abs(float(row.get("charge") or 0)) * 1_000_000)
        next_url = (body.get("links") or {}).get("next")
        pages += 1
    return total


async def fetch_spend_micros(settings: Settings, since: datetime) -> int | None:
    """SignalWire's own charges since ``since``; None when not configured or the lookup
    failed (a failed lookup must never look like spend or like no spend)."""
    import httpx

    host = (settings.signalwire_space_url or "").strip()
    project = settings.signalwire_project_id
    token = settings.signalwire_api_token.get_secret_value()
    if not (host and project and token):
        return None
    params = {"page_size": 1000, "created_after": (since - timedelta(days=1)).date().isoformat()}
    try:
        async with httpx.AsyncClient(timeout=20, auth=(project, token)) as client:
            spent = await _paged_charges(client, f"https://{host}/api/voice/logs", params, since)
            spent += await _paged_charges(
                client, f"https://{host}/api/messaging/logs", params, since
            )
            resp = await client.get(
                f"https://{host}/api/laml/2010-04-01/Accounts/{project}/IncomingPhoneNumbers.json",
                params={"PageSize": 1000},
            )
            resp.raise_for_status()
            now = _now()
            for number in resp.json().get("incoming_phone_numbers") or []:
                created = _parse_ts(number.get("date_created"))
                if created is not None:
                    spent += NUMBER_MONTHLY_MICROS * renewals_between(created, since, now)
        return spent
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        log.warning("signalwire_spend_lookup_failed")
        return None


async def _state(session: AsyncSession) -> tuple[object, dict]:
    from app.models import PlatformSetting

    row = await session.get(PlatformSetting, STATE_KEY)
    return row, dict((row.value if row is not None else None) or {})


async def _save(session: AsyncSession, row, state: dict) -> None:  # noqa: ANN001
    from app.models import PlatformSetting

    if row is None:
        session.add(PlatformSetting(key=STATE_KEY, value=state))
    else:
        row.value = state
    await session.commit()


async def is_paused(session: AsyncSession) -> bool:
    _, state = await _state(session)
    return bool(state.get("paused"))


async def paused_cached() -> bool:
    """For hot paths without a session (the carrier adapter, the voice webhook): its own
    short session, cached for PAUSE_CACHE_SECONDS. A failed read never pauses."""
    global _pause_cache
    mono = time.monotonic()
    if _pause_cache is not None and mono - _pause_cache[0] < PAUSE_CACHE_SECONDS:
        return _pause_cache[1]
    try:
        from app.db.session import get_sessionmaker

        async with get_sessionmaker()() as session:
            paused = await is_paused(session)
    except Exception:  # noqa: BLE001
        log.warning("signalwire_pause_read_failed")
        return _pause_cache[1] if _pause_cache is not None else False
    _pause_cache = (mono, paused)
    return paused


def _clear_cache() -> None:
    global _pause_cache
    _pause_cache = None


def public_state(state: dict) -> dict:
    return {
        "anchor_balance_micros": state.get("anchor_balance_micros"),
        "anchor_at": state.get("anchor_at"),
        "spent_micros": state.get("spent_micros", 0),
        "estimate_micros": state.get("estimate_micros"),
        "checked_at": state.get("checked_at"),
        "paused": bool(state.get("paused")),
        "warn_sent_at": state.get("warn_sent_at"),
        "stop_sent_at": state.get("stop_sent_at"),
        "warn_micros": WARN_MICROS,
        "stop_micros": STOP_MICROS,
    }


async def get_state(session: AsyncSession) -> dict:
    _, state = await _state(session)
    return public_state(state)


async def set_balance(session: AsyncSession, balance_micros: int, *, by: str = "") -> dict:
    """The operator read the balance off the SignalWire dashboard (after a top-up, or to
    correct drift). Re-arms both levels; lifts the pause only above the stop level."""
    row, state = await _state(session)
    now = _now().isoformat()
    state.update(
        anchor_balance_micros=int(balance_micros),
        anchor_at=now,
        spent_micros=0,
        estimate_micros=int(balance_micros),
        checked_at=now,
        anchor_by=by,
    )
    if balance_micros > WARN_MICROS:
        state["warn_sent_at"] = None
    if balance_micros > STOP_MICROS:
        state["paused"] = False
        state["stop_sent_at"] = None
    log.info("signalwire_balance_set", balance_micros=balance_micros, by=by,
             paused=bool(state.get("paused")))
    await _save(session, row, state)
    _clear_cache()
    return public_state(state)


async def _email(session: AsyncSession, settings: Settings, subject: str, body: str) -> None:
    from app.services import break_glass, mailer

    to = [OWNER_EMAIL]
    for addr in await break_glass.admin_emails(session):
        if addr.lower() not in {a.lower() for a in to}:
            to.append(addr)
    await mailer.send(settings, to, subject, body)


def _money(micros: int) -> str:
    return f"${micros / 1_000_000:,.2f}"


async def tick(session: AsyncSession, settings: Settings, *, fetch=None) -> dict[str, int]:  # noqa: ANN001
    global _last_run
    mono = time.monotonic()
    if _last_run is not None and mono - _last_run < CHECK_INTERVAL_SECONDS:
        return {}
    _last_run = mono

    row, state = await _state(session)
    anchor = state.get("anchor_balance_micros")
    anchor_at = _parse_ts(state.get("anchor_at"))
    if anchor is None or anchor_at is None:
        return {}
    spent = await (fetch or fetch_spend_micros)(settings, anchor_at)
    if spent is None:
        return {}
    estimate = int(anchor) - int(spent)
    now = _now().isoformat()
    state.update(spent_micros=int(spent), estimate_micros=estimate, checked_at=now)
    out = {"signalwire_estimate_micros": estimate}

    detail = (
        f"Estimated SignalWire balance: {_money(estimate)} (entered {_money(int(anchor))} at "
        f"{state.get('anchor_at')}, SignalWire has charged {_money(int(spent))} since)."
    )
    if estimate <= STOP_MICROS and not state.get("paused"):
        state["paused"] = True
        state["stop_sent_at"] = now
        state["warn_sent_at"] = state.get("warn_sent_at") or now
        log.error("signalwire_paused_low_balance", estimate_micros=estimate)
        await _save(session, row, state)
        _clear_cache()
        await _email(
            session, settings,
            f"SignalWire STOPPED on {settings.app_name}: balance {_money(estimate)}",
            f"{detail}\n\nIt reached {_money(STOP_MICROS)}, so SignalWire numbers are now paused: "
            "outbound calls (911 still connects), outbound texts and inbound calls on SignalWire "
            "numbers are refused. Telnyx numbers keep working.\n\n"
            "To resume after a top-up, enter the new balance in Switchboard > Profit > SignalWire.",
        )
        out["signalwire_paused"] = 1
        return out
    if estimate <= WARN_MICROS and not state.get("warn_sent_at"):
        state["warn_sent_at"] = now
        log.warning("signalwire_balance_low", estimate_micros=estimate)
        await _save(session, row, state)
        await _email(
            session, settings,
            f"SignalWire balance about to run out on {settings.app_name}: {_money(estimate)}",
            f"{detail}\n\nAt {_money(STOP_MICROS)} SignalWire numbers stop and calls must move "
            "to Telnyx numbers. Move the remaining SignalWire lines to Telnyx now, or top up "
            "SignalWire and enter the new balance in Switchboard.",
        )
        out["signalwire_balance_alerted"] = 1
        return out
    await _save(session, row, state)
    return out
