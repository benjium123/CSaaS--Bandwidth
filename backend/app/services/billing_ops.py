"""Billing v2 periodic jobs, run hourly by the sweeper. Every job opens its own session
and swallows its own failure, so one broken job never stops the others."""

from __future__ import annotations

from datetime import date, datetime, timezone

import structlog

from app.db.session import get_sessionmaker

log = structlog.get_logger("billing_ops")
#: UTC date the nightly Telnyx reconciliation last ran for (one process runs the sweeper).
_recon_done_for: date | None = None


async def hourly(settings, app_state=None) -> dict:  # noqa: ANN001
    results: dict = {}
    from app.services import payments, stripe_client

    if stripe_client.is_configured(settings):
        try:
            async with get_sessionmaker()() as session:
                results["stripe_fees_filled"] = await payments.fee_tick(session, settings)
        except Exception:
            log.exception("billing_ops.fee_tick_failed")
    # Nightly Telnyx reconciliation: once per UTC day, after 03:00 (carriers finalise late).
    global _recon_done_for  # noqa: PLW0603
    now = datetime.now(timezone.utc)
    if now.hour >= 3 and _recon_done_for != now.date():
        _recon_done_for = now.date()
        try:
            from app.services import telnyx_recon

            results["telnyx_recon"] = await telnyx_recon.nightly(settings)
        except Exception:
            log.exception("billing_ops.telnyx_recon_failed")
        try:
            from app.services import fax as fax_svc

            store = getattr(app_state, "media_store", None) if app_state is not None else None
            if store is not None:
                async with get_sessionmaker()() as session:
                    results["fax_media_purged"] = await fax_svc.purge_old_media(session, store)
        except Exception:
            log.exception("billing_ops.fax_purge_failed")
    try:
        from app.services import billing_alerts

        async with get_sessionmaker()() as session:
            results["billing_thresholds_refreshed"] = await billing_alerts.refresh_thresholds(
                session
            )
    except Exception:
        log.exception("billing_ops.thresholds_failed")
    return results
