"""One port-out PIN per workspace, set on every Telnyx number the workspace holds.

Another carrier can only take a Telnyx number away when it quotes that number's PIN
(Telnyx ``external_pin``); a wrong or missing PIN is refused by Telnyx automatically. Each
workspace gets its own PIN so one customer never learns a code that protects another
customer's numbers (all workspaces share our Telnyx account).

Storage: ``PlatformSetting["port_pin:<org_id>"] = {"enc", "fp", "created_at", "rotated_at"}``.
``enc`` is the Fernet-encrypted ``{"pin": "123456"}``; ``fp`` is a short fingerprint of the PIN
used to tell which numbers already carry the current one (``OrgNumber.provisioning
["port_pin_fp"]``), so the hourly sweep never has to read anything back from Telnyx.

Only owners and admins see the PIN, behind a fresh second factor (routes/porting.py).
SignalWire numbers are not covered: SignalWire keeps every number locked and only releases
one through its support desk, which only we (the account holder) can ask.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
import structlog

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.models import Org, OrgNumber, PlatformSetting
from app.services import credentials

log = structlog.get_logger("port_pin")

PIN_LENGTH = 6
#: Owners and admins - the same people who get billing and security alerts.
PIN_ROLES = ("owner", "admin")


def _key(org_id: uuid.UUID) -> str:
    return f"port_pin:{org_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(org_id: uuid.UUID, pin: str) -> str:
    return hashlib.sha256(f"{org_id}:{pin}".encode()).hexdigest()[:16]


def _weak(pin: str) -> bool:
    return len(set(pin)) == 1 or pin in "01234567890" or pin in "09876543210"


def new_pin() -> str:
    while True:
        pin = f"{secrets.randbelow(10**PIN_LENGTH):0{PIN_LENGTH}d}"
        if not _weak(pin):
            return pin


def account_id(org: Org) -> str:
    """Ringlite account ID, e.g. ``RL-SABINE-7Q4K``: the start of the workspace name, so a
    person can tell whose it is, plus 4 characters from the workspace id so two workspaces
    with the same name never share one. Fixed for the life of the workspace (the name part
    is taken from the slug, which does not change on rename)."""
    word = re.sub(r"[^A-Z0-9]", "", (org.slug or org.name or "").split("-")[0].upper())[:6]
    tail = base64.b32encode(hashlib.sha256(str(org.id).encode()).digest()).decode()[:4]
    return f"RL-{word or 'ORG'}-{tail}"


async def _row(session, org_id: uuid.UUID) -> PlatformSetting | None:  # noqa: ANN001
    return await session.get(PlatformSetting, _key(org_id))


async def status(session, org_id: uuid.UUID) -> dict:  # noqa: ANN001
    """What the PIN card shows without revealing the PIN."""
    row = await _row(session, org_id)
    value = dict(row.value or {}) if row is not None else {}
    numbers = await _telnyx_numbers(session, org_id)
    fp = value.get("fp")
    protected = sum(1 for n in numbers if fp and (n.provisioning or {}).get("port_pin_fp") == fp)
    return {
        "has_pin": bool(fp),
        "numbers_total": len(numbers),
        "numbers_protected": protected,
        "rotated_at": value.get("rotated_at") or value.get("created_at"),
    }


async def _store(session, settings, org_id: uuid.UUID, pin: str, actor) -> None:  # noqa: ANN001
    row = await _row(session, org_id)
    now = _now()
    value = {
        "enc": credentials.encrypt(settings, {"pin": pin}),
        "fp": fingerprint(org_id, pin),
        "created_at": (row.value or {}).get("created_at", now) if row is not None else now,
        "rotated_at": now,
    }
    if row is None:
        session.add(PlatformSetting(key=_key(org_id), value=value, updated_by=actor))
    else:
        row.value = value
        row.updated_by = actor


async def get_or_create(session, settings, org_id: uuid.UUID, actor=None) -> str:  # noqa: ANN001
    """The workspace's PIN, creating one on first use. Caller commits."""
    row = await _row(session, org_id)
    if row is not None and (row.value or {}).get("enc"):
        return str(credentials.decrypt(settings, row.value["enc"]).get("pin") or "")
    pin = new_pin()
    await _store(session, settings, org_id, pin, actor)
    return pin


async def rotate(session, settings, org_id: uuid.UUID, actor) -> str:  # noqa: ANN001
    """A new PIN. Numbers still carry the old one until ``apply_org`` runs. Caller commits."""
    pin = new_pin()
    await _store(session, settings, org_id, pin, actor)
    return pin


async def _telnyx_numbers(session, org_id: uuid.UUID) -> list[OrgNumber]:  # noqa: ANN001
    return list(
        (
            await session.execute(
                sa.select(OrgNumber)
                .where(
                    OrgNumber.org_id == org_id,
                    OrgNumber.carrier == "telnyx",
                    OrgNumber.is_active.is_(True),
                    OrgNumber.status == "active",
                )
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        )
        .scalars()
        .all()
    )


#: A number Telnyx refused is retried this long after the failure, not on every sweep -
#: unless the PIN changed since (a new PIN is always tried at once).
RETRY_AFTER = timedelta(hours=6)


def _backing_off(prov: dict, fp: str) -> bool:
    if prov.get("port_pin_error_fp") != fp:
        return False
    try:
        failed_at = datetime.fromisoformat(str(prov.get("port_pin_error_at")))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - failed_at < RETRY_AFTER


async def apply_org(session, settings, registry, org_id: uuid.UUID) -> dict:  # noqa: ANN001
    """Put the workspace PIN on each of its Telnyx numbers that does not carry it yet.
    Never raises; commits."""
    from app.services.porting import _tx, _tx_error

    counts = {"applied": 0, "failed": 0}
    carrier = registry.get("telnyx") if registry is not None else None
    if carrier is None:
        return counts
    set_org_context(session, org_id)
    numbers = await _telnyx_numbers(session, org_id)
    if not numbers:
        return counts
    try:
        pin = await get_or_create(session, settings, org_id)
    except Exception:  # noqa: BLE001 - no credential key: nothing to apply, and say so
        log.exception("port_pin_unavailable", org_id=str(org_id))
        return counts
    fp = fingerprint(org_id, pin)
    for number in numbers:
        prov = dict(number.provisioning or {})
        if prov.get("port_pin_fp") == fp:
            continue
        if not number.provider_ref or _backing_off(prov, fp):
            continue
        try:
            resp = await _tx(
                carrier, "PATCH", f"/phone_numbers/{number.provider_ref}",
                json={"external_pin": pin},
            )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)[:200]
        else:
            error = None if resp.status_code < 300 else _tx_error(resp)
        if error is None:
            prov["port_pin_fp"] = fp
            for k in ("port_pin_error", "port_pin_error_fp", "port_pin_error_at"):
                prov.pop(k, None)
            counts["applied"] += 1
        else:
            prov["port_pin_error"] = error
            prov["port_pin_error_fp"] = fp
            prov["port_pin_error_at"] = _now()
            counts["failed"] += 1
            log.warning("port_pin_apply_failed", number_id=str(number.id), error=error)
        number.provisioning = prov
    await session.commit()
    return counts


async def tick(session_factory, settings, registry) -> dict:  # noqa: ANN001
    """Sweeper: every workspace with Telnyx numbers gets a PIN, and every such number
    carries it (covers numbers bought or ported in since the last run)."""
    totals = {"applied": 0, "failed": 0}
    if registry is None or registry.get("telnyx") is None:
        return totals
    async with session_factory() as session:
        org_ids = (
            await session.execute(
                sa.select(OrgNumber.org_id)
                .where(
                    OrgNumber.carrier == "telnyx",
                    OrgNumber.is_active.is_(True),
                    OrgNumber.status == "active",
                )
                .distinct()
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
    for org_id in org_ids:
        async with session_factory() as session:
            try:
                counts = await apply_org(session, settings, registry, org_id)
            except Exception:  # noqa: BLE001 - one workspace never stops the rest
                log.exception("port_pin_tick_failed", org_id=str(org_id))
                continue
        for k in totals:
            totals[k] += counts.get(k, 0)
    return totals
