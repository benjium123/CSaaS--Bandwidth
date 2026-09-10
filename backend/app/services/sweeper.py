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
import time

import sqlalchemy as sa
import structlog

log = structlog.get_logger("sweeper")

#: Opus review B4: an every-~60s, all-orgs, trailing-7-day scan is needless load. Gated to
#: at most once per this many seconds via a plain monotonic timestamp on app.state (one
#: process, no DB round trip needed just to decide whether to run).
REPUTATION_TICK_INTERVAL_SECONDS = 3600

#: P19: provider spend rollup is derived (like usage), but there is no reason to
#: recompute every org's spend on every ~60s tick - hourly matches the reputation gate
#: above, same discipline (Opus review B4).
SPEND_TICK_INTERVAL_SECONDS = 3600

#: 8.18/4.15/6.19: arbitrary constant lock key, one per "the whole sweeper pass". Any
#: int works for pg_try_advisory_lock - it just needs to be the SAME constant every call
#: so concurrent workers contend on the identical lock.
_SWEEPER_ADVISORY_LOCK_KEY = 872341995


async def run_once(app) -> dict[str, int]:
    """Acquires a Postgres advisory lock for the WHOLE pass before running it, so a
    second sweeper worker/process cannot double-run the same pass concurrently - every
    task below assumes it is the only writer claiming its own rows this tick. A worker
    that loses the race skips this pass entirely (returns {}) rather than blocking -
    the next scheduled tick tries again. Skipped entirely on SQLite (single-process
    tests/dev only; pg_try_advisory_lock does not exist there)."""
    from app.db.session import get_sessionmaker

    lock_session = get_sessionmaker()()
    is_postgres = False
    try:
        is_postgres = lock_session.get_bind().dialect.name == "postgresql"
        if is_postgres:
            got_lock = (
                await lock_session.execute(
                    sa.select(sa.func.pg_try_advisory_lock(_SWEEPER_ADVISORY_LOCK_KEY))
                )
            ).scalar_one()
            if not got_lock:
                log.info("sweeper_pass_skipped_locked")
                return {}
        return await _run_once_locked(app)
    finally:
        if is_postgres:
            try:
                await lock_session.execute(
                    sa.select(sa.func.pg_advisory_unlock(_SWEEPER_ADVISORY_LOCK_KEY))
                )
            except Exception:
                log.exception("sweeper_advisory_unlock_failed")
                # D8: pg_try_advisory_lock is SESSION-level - if the unlock call itself
                # failed, a plain close() would return this connection to the pool
                # STILL HOLDING the lock, and the next borrower would silently inherit
                # it (every future sweeper pass on that connection blocked/skipped
                # forever). Invalidate so the underlying connection is discarded
                # instead of pooled - the lock dies with it.
                try:
                    await lock_session.invalidate()
                except Exception:
                    log.exception("sweeper_advisory_lock_session_invalidate_failed")
        await lock_session.close()


async def _run_once_locked(app) -> dict[str, int]:
    """One pass. Each task gets its own session so one failure cannot poison the others."""
    from random import Random

    from app.db.session import get_sessionmaker
    from app.services import dialer as dialer_svc
    from app.services import inbox_sla as inbox_sla_svc
    from app.services import media as media_svc
    from app.services import messaging as messaging_svc
    from app.services import notifications as notifications_svc
    from app.services import number_orders
    from app.services import outbound as outbound_svc
    from app.services import recordings as recordings_svc
    from app.services import reputation as reputation_svc
    from app.services import routing_exec as routing_exec_svc
    from app.services import scoring as scoring_svc
    from app.services import spend as spend_svc
    from app.services import usage as usage_svc
    from app.services import voicemail as voicemail_svc
    from app.services import webhooks_out as webhooks_out_svc

    store = getattr(app.state, "media_store", None)
    carrier = getattr(app.state, "carrier", None)
    registry = getattr(app.state, "carriers", None)
    results = {
        "media_fetched": 0,
        "released": 0,
        "purged": 0,
        "reprocessed": 0,
        "recordings_fetched": 0,
        "number_orders_polled": 0,
    }

    if store is not None:
        try:
            async with get_sessionmaker()() as session:
                results["media_fetched"] = await media_svc.fetch_pending_media(
                    session, store, carrier=carrier, registry=registry, settings=app.state.settings
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
                        session, store, registry, settings=app.state.settings
                    )
            except Exception:
                log.exception("sweeper_recording_fetch_failed")

        try:
            async with get_sessionmaker()() as session:
                results["purged"] = await media_svc.purge_expired_media(session, store)
        except Exception:
            log.exception("sweeper_purge_failed")

    # P18: async carrier number orders (Bandwidth's RECEIVED -> COMPLETE/FAILED) get
    # polled to a terminal state here. Org-scoped and committed per row inside
    # number_orders.py itself - this hook only owns the loop and the counter, same
    # discipline as every other task in this function.
    if registry is not None:
        try:
            async with get_sessionmaker()() as session:
                results["number_orders_polled"] = await number_orders.poll_pending_number_orders(
                    session, registry, settings=app.state.settings
                )
        except Exception:
            log.exception("sweeper_number_order_poll_failed")

    if carrier is not None:
        try:
            async with get_sessionmaker()() as session:
                # 2.3: dispatch each held row via ITS OWN carrier (registry), not
                # unconditionally through the primary.
                results["released"] = await messaging_svc.release_held_messages(
                    session, carrier, registry=registry, settings=app.state.settings
                )
        except Exception:
            log.exception("sweeper_release_failed")

    try:
        async with get_sessionmaker()() as session:
            # 2.9: re-parse via the OWNING carrier adapter, selected from the registry.
            results["reprocessed"] = await messaging_svc.reprocess_pending(
                session, registry=registry
            )
    except Exception:
        log.exception("sweeper_reprocess_failed")

    try:
        async with get_sessionmaker()() as session:
            # 2.11: re-drive messages stranded in `queued` with no hold_until after a
            # crash (added by the Area 2 batch; wiring it into the sweeper loop is this
            # batch's job).
            results["stale_queued_recovered"] = await messaging_svc.recover_stale_queued(
                session, registry=registry, settings=app.state.settings
            )
    except Exception:
        log.exception("sweeper_stale_queued_recovery_failed")

    # P11: the outbound campaign scheduler and the auto-dialer are ticks inside this same
    # loop (DR-6) - no new process, no Redis. Each gets its own session/try-except so a
    # failure in one can never poison the others, same discipline as every task above.
    # 6.18: gate on EITHER the env carrier OR a carrier registry object being present at
    # all - outbound_tick resolves the actual per-campaign carrier itself (env, or a
    # primed DB-backed org account via `registry`), so requiring the single env
    # `carrier` here used to mean a DB-only deployment (no env carrier configured
    # anywhere) could never send a single campaign even though every campaign's own org
    # had a perfectly good provider account.
    if carrier is not None or registry is not None:
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
                    registry=registry,
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

    # P26 inbox pro: three cheap, all-org passes - snoozed conversations come back when
    # they are due, inbox first-reply/resolution targets are checked, and missed inbound
    # calls become bell entries. Each gets its own session and its own try/except so one
    # failure can never poison the others, same discipline as every task above. All three
    # commit per row internally, and each is idempotent (a due snooze is cleared once,
    # sla_breached_at is stamped once, and every notification carries a dedupe key), so a
    # repeated pass is a no-op rather than a duplicate.
    try:
        async with get_sessionmaker()() as session:
            results["snoozes_reopened"] = await inbox_sla_svc.reopen_due_snoozes(session)
    except Exception:
        log.exception("sweeper_snooze_reopen_failed")

    try:
        async with get_sessionmaker()() as session:
            sla_counts = await inbox_sla_svc.sla_tick(
                session, getattr(app.state, "event_bus", None)
            )
        results["sla_breached"] = sla_counts.get("breached", 0)
    except Exception:
        log.exception("sweeper_sla_tick_failed")

    try:
        async with get_sessionmaker()() as session:
            missed_counts = await notifications_svc.missed_call_tick(
                session, getattr(app.state, "event_bus", None)
            )
        results["missed_call_notifications"] = missed_counts.get("notifications", 0)
    except Exception:
        log.exception("sweeper_missed_call_tick_failed")

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

    # P13: durable outbox fan-out + signed delivery (DR-4/DR-5), usage rollup (DR-2), and
    # LLM call scoring (DR-8, guarded on a configured key so an unconfigured deployment
    # never churns every pending call to `disabled` on every tick).
    try:
        async with get_sessionmaker()() as session:
            results["webhooks_fanned_out"] = await webhooks_out_svc.fan_out_pending_events(
                session
            )
    except Exception:
        log.exception("sweeper_webhook_fanout_failed")

    try:
        async with get_sessionmaker()() as session:
            delivery_counts = await webhooks_out_svc.delivery_tick(session, app.state.settings)
        results["webhooks_delivered"] = delivery_counts.get("delivered", 0)
    except Exception:
        log.exception("sweeper_webhook_delivery_failed")

    try:
        async with get_sessionmaker()() as session:
            usage_counts = await usage_svc.usage_tick(session)
        results["usage_orgs_rolled_up"] = usage_counts.get("orgs", 0)
    except Exception:
        log.exception("sweeper_usage_rollup_failed")

    # P19: derived provider spend rollup (today + yesterday, every org) - same hourly
    # gate discipline as reputation below: reserve the slot BEFORE running so a
    # persistent failure cannot turn this into an every-tick retry storm.
    last_spend_run = getattr(app.state, "_spend_last_run", None)
    now_monotonic = time.monotonic()
    if (
        last_spend_run is None
        or now_monotonic - last_spend_run >= SPEND_TICK_INTERVAL_SECONDS
    ):
        app.state._spend_last_run = now_monotonic
        try:
            async with get_sessionmaker()() as session:
                spend_orgs = await spend_svc.rollup_recent(session)
            results["spend_orgs_rolled_up"] = spend_orgs
        except Exception:
            log.exception("sweeper_spend_rollup_failed")

    # P14 DR-7: derived per-number reputation monitoring, one audit row per breach per
    # (org, number, UTC day) - same per-org-commit discipline as usage_tick above. Gated
    # to hourly (Opus review B4): reserve the slot BEFORE running, not just on success, so
    # a persistent failure cannot turn this into an every-tick retry storm either.
    last_reputation_run = getattr(app.state, "_reputation_last_run", None)
    now_monotonic = time.monotonic()
    if (
        last_reputation_run is None
        or now_monotonic - last_reputation_run >= REPUTATION_TICK_INTERVAL_SECONDS
    ):
        app.state._reputation_last_run = now_monotonic
        try:
            async with get_sessionmaker()() as session:
                reputation_counts = await reputation_svc.reputation_tick(session)
            results["reputation_alerts"] = reputation_counts.get("alerts", 0)
        except Exception:
            log.exception("sweeper_reputation_tick_failed")

    settings = app.state.settings
    if settings.anthropic_api_key.get_secret_value() or settings.openai_api_key.get_secret_value():
        try:
            async with get_sessionmaker()() as session:
                scoring_counts = await scoring_svc.score_pending_calls(session, settings)
            results["calls_scored"] = scoring_counts.get("done", 0)
        except Exception:
            log.exception("sweeper_call_scoring_failed")

    return results


async def sweeper_loop(app, interval_seconds: int) -> None:
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
