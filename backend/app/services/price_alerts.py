"""Price-unset alerts (P46 P2b).

A per-minute feature metric (``telephony_billing.PRICED_METRICS``) with no
``platform_prices`` row is UNSET: its usage meters at $0 and ops sees ONE open
``SecurityAlert(kind="price_unset")`` per metric in the Switchboard Alerts tab. Repeat
triggers update that alert's counters and never insert a second one. Setting the price
(``PUT /ops/console/prices/{metric}``) marks it reviewed; the hourly sweep reopens it
while any workspace can still use the unpriced feature.

Triggers: metering (``telephony_billing.charge_feature_minutes``), ops switching a
feature on, and the hourly sweep (``billing_ops.hourly``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import ALLOW_UNSCOPED_KEY
from app.models import PlatformPrice, SecurityAlert

log = structlog.get_logger("price_alerts")

KIND = "price_unset"
#: Default-OFF features ops may not switch on while their price is unset (409): nothing is
#: lost by waiting, and it stops "free recording for a month".
BLOCK_ENABLE_WHEN_UNSET = frozenset({"call_recording", "call_transcription"})
#: The alert lists at most this many workspaces (the count keeps going).
MAX_ORGS_LISTED = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def is_unset(session: AsyncSession, metric: str) -> bool:
    """True when ``metric`` is a per-minute feature metric with no platform price yet."""
    from app.services.telephony_billing import PRICED_METRICS

    if metric not in PRICED_METRICS:
        return False
    return await session.get(PlatformPrice, metric) is None


async def _open_alert(session: AsyncSession, metric: str) -> SecurityAlert | None:
    rows = (
        await session.execute(
            sa.select(SecurityAlert).where(
                SecurityAlert.kind == KIND, SecurityAlert.status == "open"
            )
        )
    ).scalars()
    for row in rows:
        if (row.detail or {}).get("metric") == metric:
            return row
    return None


async def raise_unset(
    session: AsyncSession,
    metric: str,
    *,
    org_id: uuid.UUID | None = None,
    reason: str,
    minutes: int = 0,
) -> SecurityAlert:
    """Open the metric's price_unset alert, or update the open one. Does not commit."""
    from app.services.telephony_billing import PRICED_METRICS

    now = _now().isoformat()
    row = await _open_alert(session, metric)
    if row is None:
        row = SecurityAlert(
            id=uuid.uuid4(),
            kind=KIND,
            org_id=None,
            status="open",
            detail={
                "metric": metric,
                "feature_keys": list(PRICED_METRICS.get(metric, ())),
                "first_seen": now,
            },
        )
        session.add(row)
        log.warning("price_unset_alert_opened", metric=metric, reason=reason)
    detail = dict(row.detail or {})
    detail["count"] = int(detail.get("count") or 0) + 1
    detail["unpriced_minutes"] = int(detail.get("unpriced_minutes") or 0) + max(int(minutes), 0)
    detail["last_seen"] = now
    reasons = list(detail.get("reasons") or [])
    if reason not in reasons:
        reasons.append(reason)
    detail["reasons"] = reasons
    orgs = list(detail.get("orgs") or [])
    if org_id is not None and str(org_id) not in orgs and len(orgs) < MAX_ORGS_LISTED:
        orgs.append(str(org_id))
    detail["orgs"] = orgs
    row.detail = detail  # a new dict, so the JSON column is flagged dirty
    return row


async def resolve(session: AsyncSession, metric: str, *, actor_user_id: uuid.UUID | None) -> bool:
    """The price was set: mark the metric's open alert reviewed. Does not commit."""
    row = await _open_alert(session, metric)
    if row is None:
        return False
    row.status = "reviewed"
    row.reviewed_by = actor_user_id
    row.reviewed_at = _now()
    row.review_note = "price set"
    return True


async def _enabled_anywhere(session: AsyncSession, keys: tuple[str, ...]) -> bool:
    from app.models import OrgFeature
    from app.services import entitlements

    if any(entitlements.CATALOG[k].default_enabled for k in keys if k in entitlements.CATALOG):
        return True
    found = (
        await session.execute(
            sa.select(OrgFeature.id)
            .where(OrgFeature.feature_key.in_(keys), OrgFeature.enabled.is_(True))
            .limit(1)
            .execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).first()
    return found is not None


async def sweep(session: AsyncSession) -> int:
    """Hourly: raise/refresh the alert for every unset metric some workspace can use.
    Returns how many metrics are unset and in use. Commits."""
    from app.services.telephony_billing import PRICED_METRICS

    raised = 0
    for metric, keys in PRICED_METRICS.items():
        if not await is_unset(session, metric):
            continue
        if not await _enabled_anywhere(session, keys):
            continue
        await raise_unset(session, metric, reason="hourly")
        raised += 1
    await session.commit()
    return raised
