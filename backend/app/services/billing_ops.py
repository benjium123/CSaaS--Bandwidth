"""Billing v2 periodic jobs, run hourly by the sweeper. Every job opens its own session
and swallows its own failure, so one broken job never stops the others."""

from __future__ import annotations

from datetime import date, datetime, timezone

import structlog

from app.db.session import get_sessionmaker

log = structlog.get_logger("billing_ops")
#: One-time starting credit every org gets (settings.welcome_credit_micros, $1 by default):
#: existing orgs via the hourly sweep, new ones at creation. The fixed ledger reference makes
#: it once per org, however often it runs.
WELCOME_REFERENCE = "welcome-credit"


def _welcome_amount() -> int:
    from app.config import get_active_settings

    return max(int(get_active_settings().welcome_credit_micros or 0), 0)


async def grant_welcome_credit(session, org_id) -> None:  # noqa: ANN001
    """Credit the org its one-time welcome credit. Idempotent. Does not commit."""
    from app.services import credits

    amount = _welcome_amount()
    if amount <= 0:
        return
    await credits.adjust(
        session,
        org_id,
        amount,
        reference=f"{WELCOME_REFERENCE}:{org_id}",
        note="Welcome credit",
        created_by=None,
    )


async def grant_missing_welcome_credits(session) -> int:  # noqa: ANN001
    """Give the welcome credit to every org that has not had it. Returns orgs credited."""
    import sqlalchemy as sa

    if _welcome_amount() <= 0:
        return 0

    from app.db.base import ALLOW_UNSCOPED_KEY
    from app.models import CreditLedgerEntry, Org

    # JUSTIFIED: a sweeper pass legitimately spans every tenant.
    org_ids = (
        await session.execute(
            sa.select(Org.id).execution_options(**{ALLOW_UNSCOPED_KEY: True})
        )
    ).scalars().all()
    done = set(
        (
            await session.execute(
                sa.select(CreditLedgerEntry.reference)
                .where(CreditLedgerEntry.reference.like(f"{WELCOME_REFERENCE}:%"))
                .execution_options(**{ALLOW_UNSCOPED_KEY: True})
            )
        ).scalars().all()
    )
    granted = 0
    for org_id in org_ids:
        if f"{WELCOME_REFERENCE}:{org_id}" in done:
            continue
        try:
            await grant_welcome_credit(session, org_id)
            await session.commit()
            granted += 1
        except Exception:
            await session.rollback()
            log.exception("billing_ops.welcome_credit_failed", org_id=str(org_id))
    return granted
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
        async with get_sessionmaker()() as session:
            results["welcome_credits"] = await grant_missing_welcome_credits(session)
    except Exception:
        log.exception("billing_ops.welcome_credits_failed")
    try:
        from app.services import billing_alerts

        async with get_sessionmaker()() as session:
            results["billing_thresholds_refreshed"] = await billing_alerts.refresh_thresholds(
                session
            )
    except Exception:
        log.exception("billing_ops.thresholds_failed")
    return results
