"""P42: keeps LiveKit SIP trunk `numbers` lists in sync with `org_numbers`.

Numbers used to require trunk surgery (delete + recreate, which changes trunk ids and
loses the dispatch rule) every time one was bought or released, because the LiveKit
server running tonight has no `UpdateSIPInboundTrunk`/`UpdateSIPOutboundTrunk` RPC
(`bad_route`). This module owns trunk membership from the backend's side so that stops
being a manual operator step once the server is upgraded to the v1.13 line (see
docs/PLAN_P42_TRUNK_SYNC.md).

Every function here is defensive by design: a LiveKit call that fails - because the RPC
is missing on the old server, or for any transport reason - is logged as
``trunk_sync_unavailable`` and swallowed. Provisioning and release must never fail or
roll back because trunk sync failed; `reconcile` (driven by the sweeper) is the
self-healing path that catches the trunk up once the server actually supports it.

This module never edits a trunk whose id is not in `settings` - the CRM's own LiveKit
instance's trunks are not ours to touch, and an unconfigured carrier is a silent no-op
everywhere below.
"""

from __future__ import annotations

import httpx
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY
from app.models import OrgNumber
from app.voice_plane.livekit_api import LiveKitApi, LiveKitApiError

log = structlog.get_logger("voice_plane.trunk_sync")

#: Carriers this module knows how to sync. Any other carrier name is a no-op everywhere.
_CARRIERS = ("telnyx", "signalwire")

#: What "LiveKit failed" means here: the RPC missing (old server, LiveKitApiError
#: bad_route), any other non-2xx, or the server unreachable. All swallowed, all logged.
_SYNC_ERRORS = (LiveKitApiError, httpx.HTTPError)


def trunk_ids_for(carrier: str, settings) -> tuple[str, str]:
    """(inbound_trunk_id, outbound_trunk_id) configured for `carrier`.

    Empty string for either half means "not configured" - callers treat that half as
    absent, never as an id to call LiveKit with. An unknown carrier name, or a carrier
    with BOTH ids empty, returns ("", "")."""
    if carrier == "telnyx":
        inbound = settings.livekit_sip_telnyx_inbound_trunk_id
        outbound = settings.livekit_sip_outbound_trunk_id
    elif carrier == "signalwire":
        inbound = settings.livekit_sip_signalwire_inbound_trunk_id
        outbound = settings.livekit_sip_signalwire_trunk_id
    else:
        return ("", "")

    inbound = inbound or ""
    outbound = outbound or ""
    if not inbound and not outbound:
        return ("", "")
    return (inbound, outbound)


async def _trunk_numbers(lk: LiveKitApi, get_fn, trunk_id: str) -> list[str]:
    trunk = await get_fn(trunk_id)
    return (trunk or {}).get("trunk", {}).get("numbers") or []


async def ensure_number(lk: LiveKitApi | None, settings, carrier: str, e164: str) -> bool:
    """Adds `e164` to both configured trunks for `carrier` (read-then-add, so an
    already-present number never generates an update call). Never raises: a LiveKit
    failure - old server `bad_route`, or a transport error - logs
    `trunk_sync_unavailable` and returns False, same as an unconfigured carrier or a
    number that was already present everywhere."""
    if lk is None:
        return False

    inbound_id, outbound_id = trunk_ids_for(carrier, settings)
    if not inbound_id and not outbound_id:
        return False

    updated = False
    try:
        if inbound_id:
            numbers = await _trunk_numbers(lk, lk.get_sip_inbound_trunk, inbound_id)
            if e164 not in numbers:
                await lk.update_sip_inbound_trunk(inbound_id, {"numbers": {"add": [e164]}})
                updated = True

        if outbound_id:
            numbers = await _trunk_numbers(lk, lk.get_sip_outbound_trunk, outbound_id)
            if e164 not in numbers:
                await lk.update_sip_outbound_trunk(outbound_id, {"numbers": {"add": [e164]}})
                updated = True
    except _SYNC_ERRORS as exc:
        log.warning("trunk_sync_unavailable", carrier=carrier, e164=e164, error=str(exc))
        return False

    return updated


async def remove_number(lk: LiveKitApi | None, settings, carrier: str, e164: str) -> bool:
    """Mirror of `ensure_number`: removes `e164` from both configured trunks (read-then-
    remove, so an already-absent number never generates an update call). Same
    never-raises contract."""
    if lk is None:
        return False

    inbound_id, outbound_id = trunk_ids_for(carrier, settings)
    if not inbound_id and not outbound_id:
        return False

    updated = False
    try:
        if inbound_id:
            numbers = await _trunk_numbers(lk, lk.get_sip_inbound_trunk, inbound_id)
            if e164 in numbers:
                await lk.update_sip_inbound_trunk(inbound_id, {"numbers": {"remove": [e164]}})
                updated = True

        if outbound_id:
            numbers = await _trunk_numbers(lk, lk.get_sip_outbound_trunk, outbound_id)
            if e164 in numbers:
                await lk.update_sip_outbound_trunk(outbound_id, {"numbers": {"remove": [e164]}})
                updated = True
    except _SYNC_ERRORS as exc:
        log.warning("trunk_sync_unavailable", carrier=carrier, e164=e164, error=str(exc))
        return False

    return updated


async def reconcile(session: AsyncSession, lk: LiveKitApi | None, settings) -> dict[str, int]:
    """Self-healing pass: sets each configured carrier's trunk(s) to EXACTLY that
    carrier's active `org_numbers` e164s (`numbers.set`, not add/remove - this is what
    fixes a trunk that drifted, and what fixes the box on first deploy of a new server).

    Crosses every org on purpose (trunk membership is not tenant data); skips a carrier
    with no configured trunk id entirely - no query, no call, no log line. Returns a
    count per carrier that was actually attempted, for the sweeper's `sweeper_pass` log
    line - counted even if the LiveKit call itself failed, since the count is "numbers
    reconcile tried to set", not "numbers LiveKit confirmed"."""
    if lk is None:
        return {}

    results: dict[str, int] = {}

    for carrier in _CARRIERS:
        inbound_id, outbound_id = trunk_ids_for(carrier, settings)
        if not inbound_id and not outbound_id:
            continue

        rows = (
            await session.execute(
                sa.select(OrgNumber.e164)
                .where(
                    OrgNumber.carrier == carrier,
                    OrgNumber.is_active.is_(True),
                    OrgNumber.status == "active",
                )
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
        e164s = sorted(set(rows))
        results[carrier] = len(e164s)

        try:
            if inbound_id:
                await lk.update_sip_inbound_trunk(inbound_id, {"numbers": {"set": e164s}})
            if outbound_id:
                await lk.update_sip_outbound_trunk(outbound_id, {"numbers": {"set": e164s}})
        except _SYNC_ERRORS as exc:
            log.warning("trunk_sync_unavailable", carrier=carrier, error=str(exc))

    return results
