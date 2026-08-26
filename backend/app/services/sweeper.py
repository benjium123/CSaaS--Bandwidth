"""The interim sweeper.

Four things now need periodic driving: inbound media fetching, held-message release,
expired-media purging, and P1's dormant event reprocessing. P11 brings a real Redis-backed
scheduler; until then this 40-line asyncio loop does the job.

**This loop is throwaway by design.** The *functions* it calls are the seam P11's scheduler
will call instead — the loop itself is what gets deleted. That is why every function it
drives takes an explicit session and is independently testable: the tests never touch this
loop, they call the functions directly.

Every exception is logged and swallowed. A sweeper that dies takes the whole background
pipeline with it.
"""

from __future__ import annotations

import asyncio

import structlog

log = structlog.get_logger("sweeper")


async def run_once(app) -> dict[str, int]:  # noqa: ANN001 - FastAPI app
    """One pass. Each task gets its own session so one failure cannot poison the others."""
    from app.db.session import get_sessionmaker
    from app.services import media as media_svc
    from app.services import messaging as messaging_svc

    store = getattr(app.state, "media_store", None)
    carrier = getattr(app.state, "carrier", None)
    results = {"media_fetched": 0, "released": 0, "purged": 0, "reprocessed": 0}

    if store is not None:
        try:
            async with get_sessionmaker()() as session:
                results["media_fetched"] = await media_svc.fetch_pending_media(
                    session, store, carrier=carrier
                )
        except Exception:
            log.exception("sweeper_media_fetch_failed")

        try:
            async with get_sessionmaker()() as session:
                results["purged"] = await media_svc.purge_expired_media(session, store)
        except Exception:
            log.exception("sweeper_purge_failed")

    if carrier is not None:
        try:
            async with get_sessionmaker()() as session:
                results["released"] = await messaging_svc.release_held_messages(
                    session, carrier
                )
        except Exception:
            log.exception("sweeper_release_failed")

    try:
        async with get_sessionmaker()() as session:
            results["reprocessed"] = await messaging_svc.reprocess_pending(session)
    except Exception:
        log.exception("sweeper_reprocess_failed")

    return results


async def sweeper_loop(app, interval_seconds: int) -> None:  # noqa: ANN001
    log.info("sweeper_started", interval_seconds=interval_seconds)
    try:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                results = await run_once(app)
                if any(results.values()):
                    log.info("sweeper_pass", **results)
            except Exception:
                # Belt and braces: run_once already swallows, but the loop must survive
                # anything at all.
                log.exception("sweeper_pass_failed")
    except asyncio.CancelledError:
        log.info("sweeper_stopped")
        raise
