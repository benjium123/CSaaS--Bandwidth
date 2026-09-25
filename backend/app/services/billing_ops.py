"""Billing v2 periodic jobs, run hourly by the sweeper. Every job opens its own session
and swallows its own failure, so one broken job never stops the others."""

from __future__ import annotations

import structlog

from app.db.session import get_sessionmaker

log = structlog.get_logger("billing_ops")


async def hourly(settings) -> dict:  # noqa: ANN001
    results: dict = {}
    from app.services import payments, stripe_client

    if stripe_client.is_configured(settings):
        try:
            async with get_sessionmaker()() as session:
                results["stripe_fees_filled"] = await payments.fee_tick(session, settings)
        except Exception:
            log.exception("billing_ops.fee_tick_failed")
    try:
        from app.services import billing_alerts

        async with get_sessionmaker()() as session:
            results["billing_thresholds_refreshed"] = await billing_alerts.refresh_thresholds(
                session
            )
    except Exception:
        log.exception("billing_ops.thresholds_failed")
    return results
