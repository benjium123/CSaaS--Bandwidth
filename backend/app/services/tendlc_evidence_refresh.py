"""Keep Telnyx 10DLC approval evidence fresh (owner order 2026-10-20).

A Telnyx number may send only while its campaign carries approval evidence younger than
TELNYX_APPROVAL_MAX_AGE_SECONDS (7 days by default). Until now only the operator's manual
"refresh from Telnyx" renewed it, so every Telnyx number stopped texting a week after the
last click. This tick does the same thing on its own once a day at 06:00 Central (Texas):
for each locally approved campaign with a Telnyx reference it makes ONE read-only GET and
records fresh ``approved`` evidence, or ``revoked`` evidence when Telnyx no longer approves
it - exactly what POST /campaigns/{id}/refresh-telnyx does. It never files, never spends
and never changes a campaign's local status. A failed lookup changes nothing for that
campaign (the old evidence simply ages out as before).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import set_org_context
from app.models.numbers import Campaign

log = structlog.get_logger("tendlc_evidence_refresh")

RUN_ZONE = ZoneInfo("America/Chicago")
#: Daily run time, Central. A restart after this hour runs once that day, so no day is missed.
RUN_AT = dtime(6, 0)

_last_run_day: date | None = None


def is_due(now: datetime, last_run_day: date | None) -> bool:
    """True once per Central day, at or after RUN_AT."""
    local = now.astimezone(RUN_ZONE)
    return local.time() >= RUN_AT and last_run_day != local.date()


async def tick(
    session: AsyncSession,
    settings: Any,
    *,
    http_client: Any = None,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, int]:
    """Refresh evidence for every approved Telnyx campaign; once a day at 06:00 Central."""
    global _last_run_day
    now = now or datetime.now(timezone.utc)
    if not force and not is_due(now, _last_run_day):
        return {}
    _last_run_day = now.astimezone(RUN_ZONE).date()

    from app.api.routes.registration import (
        _apply_refresh_evidence,
        _carrier_id_matches,
        _carrier_ref,
        _resolve_telnyx_settings,
        _telnyx_api_key,
    )
    from app.providers.telnyx.registration import TelnyxRegistrationClient
    from app.providers.telnyx.registration_status import APPROVED, map_campaign_status

    # JUSTIFIED allow_unscoped: a platform tick lists every org's approved campaigns; each
    # one is then loaded and written under its own org context below.
    rows = (
        await session.execute(
            sa.select(Campaign.id, Campaign.org_id)
            .where(Campaign.status == APPROVED)
            .execution_options(allow_unscoped=True)
        )
    ).all()

    refreshed = revoked = failed = 0
    for campaign_id, org_id in rows:
        set_org_context(session, org_id)
        try:
            campaign = await session.get(Campaign, campaign_id)
            carrier_id = _carrier_ref(campaign) if campaign is not None else None
            if carrier_id is None:
                continue
            resolved = await _resolve_telnyx_settings(session, settings)
            client = TelnyxRegistrationClient(
                api_key=_telnyx_api_key(resolved), client=http_client
            )
            payload = await client.get_campaign(carrier_id)
            if not _carrier_id_matches(payload, carrier_id, "campaignId", "id"):
                raise ValueError("Telnyx returned a different campaign")
            approved = map_campaign_status(payload) == APPROVED
            _apply_refresh_evidence(campaign, carrier_approved=approved)
            await session.commit()
            if approved:
                refreshed += 1
            else:
                revoked += 1
                log.warning("tendlc_evidence_revoked", campaign_id=str(campaign_id))
        except Exception:  # noqa: BLE001 - one bad campaign must not stop the others
            await session.rollback()
            failed += 1
            log.warning("tendlc_evidence_refresh_failed", campaign_id=str(campaign_id))
        finally:
            set_org_context(session, None)

    log.info(
        "tendlc_evidence_refresh_done",
        refreshed=refreshed, revoked=revoked, failed=failed,
    )
    return {
        "tendlc_evidence_refreshed": refreshed,
        "tendlc_evidence_revoked": revoked,
        "tendlc_evidence_failed": failed,
    }
