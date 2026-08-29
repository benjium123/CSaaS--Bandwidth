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
    from random import Random

    from app.db.session import get_sessionmaker
    from app.services import dialer as dialer_svc
    from app.services import media as media_svc
    from app.services import messaging as messaging_svc
    from app.services import outbound as outbound_svc
    from app.services import recordings as recordings_svc
    from app.services import routing_exec as routing_exec_svc
    from app.services import voicemail as voicemail_svc

    store = getattr(app.state, "media_store", None)
    carrier = getattr(app.state, "carrier", None)
    registry = getattr(app.state, "carriers", None)
    results = {
        "media_fetched": 0,
        "released": 0,
        "purged": 0,
        "reprocessed": 0,
        "recordings_fetched": 0,
    }

    if store is not None:
        try:
            async with get_sessionmaker()() as session:
                results["media_fetched"] = await media_svc.fetch_pending_media(
                    session, store, carrier=carrier
                )
        except Exception:
            log.exception("sweeper_media_fetch_failed")

        if registry is not None:
            # F6/F7/F16: the voice webhook path only upserts a `pending` CallRecording row
            # - this is what actually fetches it, same "pending row IS the queue" shape as
            # inbound media fetch above.
            try:
                async with get_sessionmaker()() as session:
                    results["recordings_fetched"] = await recordings_svc.fetch_pending_recordings(
                        session, store, registry
                    )
            except Exception:
                log.exception("sweeper_recording_fetch_failed")

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

    # P11: the outbound campaign scheduler and the auto-dialer are ticks inside this same
    # loop (DR-6) - no new process, no Redis. Each gets its own session/try-except so a
    # failure in one can never poison the others, same discipline as every task above.
    if carrier is not None:
        try:
            async with get_sessionmaker()() as session:
                outbound_counts = await outbound_svc.outbound_tick(
                    session, carrier, app.state.settings, Random(), registry=registry
                )
            results["outbound_sent"] = outbound_counts.get("sent", 0)
        except Exception:
            log.exception("sweeper_outbound_tick_failed")

    livekit = getattr(app.state, "livekit", None)
    if livekit is not None:
        try:
            async with get_sessionmaker()() as session:
                dial_counts = await dialer_svc.dialer_tick(
                    session,
                    livekit,
                    app.state.settings,
                    getattr(app.state, "event_bus", None),
                    Random(),
                )
            results["dialer_connected"] = dial_counts.get("connected", 0)
        except Exception:
            log.exception("sweeper_dialer_tick_failed")

    # P12: IVR ring-group/queue stepping, then voicemail recording linking + transcription.
    try:
        async with get_sessionmaker()() as session:
            routing_counts = await routing_exec_svc.routing_tick(
                session, getattr(app.state, "event_bus", None)
            )
        results["routing_offered"] = routing_counts.get("offered", 0)
        results["routing_abandoned"] = routing_counts.get("abandoned", 0)
    except Exception:
        log.exception("sweeper_routing_tick_failed")

    if store is not None:
        try:
            async with get_sessionmaker()() as session:
                results["voicemails_linked"] = await voicemail_svc.link_pending_recordings(session)
        except Exception:
            log.exception("sweeper_voicemail_link_failed")

        try:
            async with get_sessionmaker()() as session:
                vm_counts = await voicemail_svc.transcribe_pending(
                    session,
                    store,
                    deepgram_api_key=app.state.settings.deepgram_api_key.get_secret_value(),
                )
            results["voicemails_transcribed"] = vm_counts.get("done", 0)
        except Exception:
            log.exception("sweeper_voicemail_transcribe_failed")

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
