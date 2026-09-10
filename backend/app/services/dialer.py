"""Auto-dialer: dial-campaign enqueue and the sweeper tick (phase-11-plan DR-10..DR-13).

DR-13: the dial seam is injectable. Every actual dial goes through the module-level
:func:`_start_call`, which production wraps around
``voice_plane.service.start_room_call``; tests replace this function wholesale with a
deterministic fake that resolves to connected/no_answer/voicemail/busy - no LiveKit
dependency in unit tests, and no parallel re-implementation of the SIP dial itself.

DR-11: compliance runs BEFORE every dial, through the SAME primitives the SMS gate uses
(``app.compliance.service`` / ``app.compliance.quiet_hours``) - this module never gets its
own copy of opt-out/DNC/quiet-hours logic.

DR-12: no_answer/busy/failed are retried up to ``campaign.max_attempts``, spaced
``campaign.retry_backoff_minutes`` apart. An AMD "machine" verdict is never retried - it is
a completed contact, just not a human one.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from random import Random

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance import quiet_hours as qh
from app.compliance import service as compliance_svc
from app.compliance.gate import _contact_timezone as _gate_contact_timezone
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ConflictError, ValidationFailedError
from app.models import (
    DIALER_MODES,
    Call,
    ContactList,
    ContactListRow,
    DialAttempt,
    OrgNumber,
    OutboundCampaign,
)
from app.providers import registry_org
from app.services import credentials as credential_svc
from app.services import pacing, smart_routing
from app.services.outbox import record_platform_event
from app.services.sender import pick_deterministic
from app.voice_plane import service as voice_plane_svc

log = structlog.get_logger("dialer")

DIALER_TICK_BATCH = 25
STALE_DIALING_MINUTES = 5
CAP_WINDOW_HOURS = 26
#: Both channels the DIALER owns. "ai_calls" dials exactly like "voice" - same claim,
#: same compliance precheck, same pacing, same retry policy - and differs only in who
#: talks: an assistant worker is dispatched into the room instead of ringing a person.
DIALER_CHANNELS = ("voice", "ai_calls")
#: 6.16: enqueue_dial_rows commits (and frees its identity map) every this many rows.
ENQUEUE_BATCH = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_sqlite(session: AsyncSession) -> bool:
    return session.get_bind().dialect.name == "sqlite"


def _bind(session: AsyncSession, moment: datetime) -> datetime:
    return moment.replace(tzinfo=None) if _is_sqlite(session) else moment


# --------------------------------------------------------------------------------------
# Campaign lifecycle (start only - pause/cancel are channel-agnostic, see services.outbound)
# --------------------------------------------------------------------------------------
async def enqueue_dial_rows(session: AsyncSession, campaign: OutboundCampaign) -> int:
    """Enqueue every ACCEPTED row of the campaign's list as a ``dial_attempts`` row.
    UNIQUE(campaign_id, e164) makes a double-enqueue impossible at the database."""
    existing = set(
        (
            await session.execute(
                sa.select(DialAttempt.e164).where(DialAttempt.campaign_id == campaign.id)
            )
        )
        .scalars()
        .all()
    )
    # 6.16: same fix as outbound.py's enqueue_campaign_rows - column-projected select,
    # batched commit + expunge every ENQUEUE_BATCH rows.
    stmt = sa.select(ContactListRow.id, ContactListRow.contact_id, ContactListRow.e164).where(
        ContactListRow.list_id == campaign.list_id, ContactListRow.status == "accepted"
    )
    created = 0
    since_commit = 0
    for row_id, contact_id, e164 in (await session.execute(stmt)).all():
        if e164 in existing:
            continue
        session.add(
            DialAttempt(
                id=uuid.uuid4(),
                org_id=campaign.org_id,
                campaign_id=campaign.id,
                row_id=row_id,
                contact_id=contact_id,
                e164=e164,
                status="queued",
            )
        )
        existing.add(e164)
        created += 1
        since_commit += 1
        if since_commit >= ENQUEUE_BATCH:
            await session.commit()
            session.expunge_all()
            since_commit = 0
    await session.commit()
    return created


async def start_dial_campaign(
    session: AsyncSession, campaign: OutboundCampaign
) -> OutboundCampaign:
    if campaign.channel not in DIALER_CHANNELS:
        raise ValidationFailedError("This campaign is not a calling campaign.")
    if campaign.status not in ("draft", "scheduled", "paused"):
        raise ConflictError(f"Cannot start a campaign in status {campaign.status!r}")
    if campaign.dialer_mode not in DIALER_MODES:
        raise ValidationFailedError(f"dialer_mode must be one of: {', '.join(DIALER_MODES)}")
    contact_list = await session.get(ContactList, campaign.list_id)
    if contact_list is None or contact_list.status != "ready":
        raise ValidationFailedError("The campaign's list is not ready yet")
    await enqueue_dial_rows(session, campaign)
    # 6.6: mirror services.outbound.start_campaign - a future start_at parks the
    # campaign as "scheduled" instead of dialing immediately.
    start_at = campaign.start_at
    if start_at is not None and start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=timezone.utc)
    campaign.status = "scheduled" if (start_at is not None and start_at > _now()) else "running"
    await session.commit()
    return campaign


# --------------------------------------------------------------------------------------
# The dial seam (DR-13)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class DialOutcome:
    """What one dial attempt resolved to.

    ``status`` is one of connected/no_answer/busy/failed. ``amd_verdict`` (human/machine/
    None) is recorded verbatim and is orthogonal to ``status`` - a machine can still
    "connect" (the audio path opens) before AMD classifies it (the P9 seam); a
    ``connected`` outcome with ``amd_verdict == "machine"`` is what the tick turns into a
    ``voicemail`` disposition, below.
    """

    status: str
    call_id: uuid.UUID | None = None
    amd_verdict: str | None = None


async def _start_call(
    session: AsyncSession,
    settings,  # noqa: ANN001 - app.config.Settings
    bus,  # noqa: ANN001 - EventBus
    api,  # noqa: ANN001 - LiveKitApi | None
    *,
    org_id: uuid.UUID,
    to_e164: str,
    from_e164: str,
    identity: str,
    agent_profile_id: uuid.UUID | None = None,
    is_test: bool = False,
) -> DialOutcome:
    """Module-level indirection (DR-13). Production wraps
    ``voice_plane.service.start_room_call`` and waits for its background dial task to
    resolve, since B2 (no SIP trunk) blocks live verification of this path end to end;
    tests replace this whole function with a deterministic fake instead."""
    if api is None:
        return DialOutcome(status="failed")
    call, leg, _room, _token = await voice_plane_svc.start_room_call(
        session,
        api,
        settings,
        bus,
        org_id=org_id,
        to=to_e164,
        from_e164=from_e164,
        identity=identity,
    )
    # P21: the dialer dials over the LiveKit SIP trunk, never a provider API, so there is
    # no ranked plan to walk and nothing to explain about carrier choice. Record the one
    # honest sentence for this path and skip ranking entirely (phase-21-plan design 3).
    call.route_reason = smart_routing.LIVEKIT_TRUNK_REASON
    if agent_profile_id is not None:
        call.extra = {
            **(call.extra or {}),
            "assistant": {"profile_id": str(agent_profile_id), "is_test": bool(is_test)},
        }
        # The worker asks for its config as soon as it joins the room, so this marker
        # must be committed before the dial task is awaited.
        await session.commit()
        from app.services import assistant_dispatch

        await assistant_dispatch.dispatch_into_room(session, api, settings, call)
    # 3.9: await only THIS call's own dial task, not the test-only global set - waiting
    # on every in-flight dial serializes concurrent calls on each other.
    await voice_plane_svc.wait_for_pending_dial_task(call.id)
    await session.refresh(leg)
    if leg.status == "answered":
        return DialOutcome(status="connected", call_id=call.id, amd_verdict=leg.amd_result)
    if leg.hangup_cause == "no_answer":
        return DialOutcome(status="no_answer", call_id=call.id)
    return DialOutcome(status="failed", call_id=call.id)


# --------------------------------------------------------------------------------------
# The sweeper tick
# --------------------------------------------------------------------------------------
async def _running_voice_campaigns(
    session: AsyncSession, now: datetime
) -> list[OutboundCampaign]:
    stmt = (
        sa.select(OutboundCampaign)
        .where(
            OutboundCampaign.channel.in_(DIALER_CHANNELS),
            sa.or_(
                OutboundCampaign.status == "running",
                sa.and_(
                    OutboundCampaign.status == "scheduled",
                    OutboundCampaign.start_at <= _bind(session, now),
                ),
            ),
        )
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    return list((await session.execute(stmt)).scalars().all())


async def _requeue_stale_dialing(session: AsyncSession, now: datetime) -> int:
    cutoff = _bind(session, now - timedelta(minutes=STALE_DIALING_MINUTES))
    stmt = (
        sa.select(DialAttempt)
        .where(
            DialAttempt.status == "dialing",
            DialAttempt.call_id.is_(None),
            DialAttempt.updated_at < cutoff,
        )
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        set_org_context(session, row.org_id)
        # 6.4: mirror outbound.py's own requeue attempt cap - a row stuck "dialing" with
        # no call_id (crashed dial task) must not requeue forever. Same semantics as a
        # normal no_answer/busy/failed retry exhaustion.
        campaign = await session.get(OutboundCampaign, row.campaign_id)
        row.attempts += 1
        if campaign is not None and row.attempts >= campaign.max_attempts:
            row.status = "failed"
            # disposition is sa.String(32) - keep it short.
            row.disposition = "requeue_attempts_exhausted"
        else:
            row.status = "queued"
        await session.commit()
    return len(rows)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips DateTime(timezone=True) as naive; ``pacing.warmup_daily_cap``
    compares its ``warmup_started_at`` argument against an aware ``now``, so a naive
    value read back from the ORM must be normalized first (mirrors outbound.py)."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def _dial_recent_count(
    session: AsyncSession, org_id: uuid.UUID, from_e164: str, since: datetime
) -> int:
    stmt = sa.select(sa.func.count()).select_from(Call).where(
        Call.org_id == org_id,
        Call.our_e164 == from_e164,
        Call.direction == "outbound",
        Call.created_at >= _bind(session, since),
    )
    return (await session.execute(stmt)).scalar_one()


async def _last_dial_at(
    session: AsyncSession, org_id: uuid.UUID, from_e164: str
) -> datetime | None:
    stmt = sa.select(sa.func.max(Call.created_at)).where(
        Call.org_id == org_id, Call.our_e164 == from_e164, Call.direction == "outbound"
    )
    value = (await session.execute(stmt)).scalar_one_or_none()
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


async def _has_pending_dial_rows(session: AsyncSession, campaign_id: uuid.UUID) -> bool:
    stmt = (
        sa.select(DialAttempt.id)
        .where(
            DialAttempt.campaign_id == campaign_id,
            DialAttempt.status.in_(("queued", "dialing")),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _claim_due_rows(
    session: AsyncSession, campaign_id: uuid.UUID, now: datetime, limit: int
) -> list[DialAttempt]:
    if limit <= 0:
        return []
    stmt = (
        sa.select(DialAttempt)
        .where(
            DialAttempt.campaign_id == campaign_id,
            DialAttempt.status == "queued",
            sa.or_(
                DialAttempt.next_attempt_at.is_(None),
                DialAttempt.next_attempt_at <= _bind(session, now),
            ),
        )
        .order_by(DialAttempt.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _compliance_precheck(
    session: AsyncSession, e164: str, org_id: uuid.UUID, now: datetime
) -> tuple[bool, datetime | None]:
    """Reuses the SAME primitives the SMS gate uses (DR-11): opt-out -> DNC -> quiet
    hours. Returns (allowed, defer_until)."""
    if await compliance_svc.is_opted_out(session, e164):
        return False, None
    if await compliance_svc.is_dnc(session, e164):
        return False, None
    settings = await compliance_svc.get_settings(session, org_id)
    if not settings.quiet_hours_enforced:
        return True, None
    tz = await _gate_contact_timezone(session, e164)
    result = qh.evaluate(
        e164,
        contact_timezone=tz,
        window_start=settings.window_start,
        window_end=settings.window_end,
        now=now,
    )
    if not result.allowed:
        return False, result.not_before
    return True, None


async def _predictive_coefficient(session: AsyncSession, campaign_id: uuid.UUID) -> float:
    stmt = (
        sa.select(DialAttempt.status, sa.func.count())
        .where(
            DialAttempt.campaign_id == campaign_id,
            DialAttempt.status.notin_(("queued", "dialing")),
        )
        .group_by(DialAttempt.status)
    )
    result_counts = dict((await session.execute(stmt)).all())
    placed = sum(result_counts.values())
    abandoned = result_counts.get("abandoned", 0)
    return pacing.predictive_coefficient(placed, abandoned)


def _dial_from_number(
    campaign: OutboundCampaign, numbers: list[OrgNumber], contact_e164: str
) -> str | None:
    pool = [n.e164 for n in numbers if not campaign.from_numbers or n.e164 in campaign.from_numbers]
    if not pool:
        return None
    if campaign.local_presence:
        # DR-10: prefer a from-number sharing the contact's area code when the org pool
        # has one; otherwise fall back to normal selection. No number renting, no pattern
        # beyond that - deliberately minimal (regulator-flagged feature).
        npa = qh.npa_of(contact_e164)
        if npa:
            local = [e164 for e164 in pool if qh.npa_of(e164) == npa]
            if local:
                return pick_deterministic(contact_e164, local)
    return pick_deterministic(contact_e164, pool)


def _apply_outcome(
    row: DialAttempt, outcome: DialOutcome, campaign: OutboundCampaign, moment: datetime
) -> str:
    """Apply one dial outcome to its row (shared by :func:`dialer_tick`'s per-row loop and
    :func:`dial_next`'s single-row path - one mapping, not two). Does NOT commit.

    Returns a counts key: connected/voicemail/no_answer/busy/failed, or
    ``"retry_scheduled"`` when a retryable outcome was NOT yet exhausted - that case is
    deliberately not one of the terminal counters (it matches the original per-tick
    counting behaviour, which never counted an in-flight retry as an outcome).
    """
    row.attempts += 1
    row.call_id = outcome.call_id
    row.amd_verdict = outcome.amd_verdict
    if outcome.status == "connected" and outcome.amd_verdict == "machine":
        row.status = "voicemail"
        row.disposition = "voicemail"
        return "voicemail"
    if outcome.status == "connected":
        row.status = "connected"
        return "connected"
    if outcome.status in ("no_answer", "busy", "failed"):
        if row.attempts >= campaign.max_attempts:
            row.status = outcome.status
            return outcome.status
        row.status = "queued"
        row.next_attempt_at = moment + timedelta(minutes=campaign.retry_backoff_minutes)
        return "retry_scheduled"
    row.status = "failed"  # pragma: no cover - defensive; DialOutcome.status is a closed set
    return "failed"


async def dial_next(
    session: AsyncSession,
    api,  # noqa: ANN001 - LiveKitApi | None
    settings,  # noqa: ANN001 - app.config.Settings
    bus,  # noqa: ANN001 - EventBus
    campaign: OutboundCampaign,
    *,
    now: datetime | None = None,
) -> DialAttempt | None:
    """Preview mode's manual trigger (DR-10: "the agent explicitly launches each call").
    Without this, a preview campaign is a trap - `dialer_tick` deliberately never auto-
    dials it, so nothing would ever get through. Claims and dials exactly ONE due row,
    running the SAME compliance precheck and outcome mapping as every other mode - a
    batch-of-one, not a parallel implementation. Returns None when nothing was due."""
    if campaign.channel not in DIALER_CHANNELS:
        raise ValidationFailedError("This campaign is not a calling campaign.")
    if campaign.status != "running":
        raise ConflictError(f"Cannot dial on a campaign in status {campaign.status!r}")

    moment = now or _now()
    set_org_context(session, campaign.org_id)

    rows = await _claim_due_rows(session, campaign.id, moment, 1)
    if not rows:
        return None
    row = rows[0]

    allowed, defer_until = await _compliance_precheck(session, row.e164, campaign.org_id, moment)
    if not allowed and defer_until is not None:
        row.next_attempt_at = defer_until
        await session.commit()
        return row
    if not allowed:
        row.status = "failed"
        row.disposition = "blocked"
        await session.commit()
        return row

    numbers = list(
        (await session.execute(sa.select(OrgNumber).where(OrgNumber.is_active.is_(True))))
        .scalars()
        .all()
    )
    from_e164 = _dial_from_number(campaign, numbers, row.e164)
    if from_e164 is None:
        return row  # nothing to dial from; row stays queued for a later attempt

    row.status = "dialing"
    await session.commit()

    # DR-13: existing voice tests replace _start_call with a fake whose signature has no
    # agent_profile_id keyword - pass it only for an ai_calls campaign so plain voice
    # campaigns continue to call the seam exactly as before.
    assistant_kwargs = (
        {"agent_profile_id": campaign.agent_profile_id}
        if campaign.channel == "ai_calls"
        else {}
    )
    outcome = await _start_call(
        session,
        settings,
        bus,
        api,
        org_id=campaign.org_id,
        to_e164=row.e164,
        from_e164=from_e164,
        identity=f"dialer-{row.id}",
        **assistant_kwargs,
    )
    _apply_outcome(row, outcome, campaign, moment)
    await session.commit()
    return row


async def dialer_tick(
    session: AsyncSession,
    api,  # noqa: ANN001 - LiveKitApi | None
    settings,  # noqa: ANN001 - app.config.Settings
    bus,  # noqa: ANN001 - EventBus
    rng: Random,
    *,
    now: datetime | None = None,
    batch: int = DIALER_TICK_BATCH,
    registry=None,  # noqa: ANN001 - CarrierRegistry; DB-backed org registry priming
) -> dict[str, int]:
    """One pass over every ``running`` voice campaign."""
    moment = now or _now()
    counts = {
        "requeued_stale": await _requeue_stale_dialing(session, moment),
        "connected": 0,
        "no_answer": 0,
        "busy": 0,
        "failed": 0,
        "voicemail": 0,
        "abandoned": 0,
        "deferred": 0,
        "completed_campaigns": 0,
    }
    remaining = batch
    # 6.20: keyed on (org_id, from_e164) - see outbound.py's identical pace_cache note.
    pace_cache: dict[tuple[uuid.UUID, str], dict] = {}

    for campaign in await _running_voice_campaigns(session, moment):
        if remaining <= 0:
            break
        set_org_context(session, campaign.org_id)

        # 4.2: prime the org's DB-backed carrier registry the same way outbound.py does
        # for SMS - the sweeper otherwise runs with CURRENT_ORG_ID=None and every voice
        # dial falls back to env credentials instead of this org's own account.
        org_token = registry_org.CURRENT_ORG_ID.set(campaign.org_id)
        try:
            if (
                settings is not None
                and registry is not None
                and credential_svc.master_key_present(settings)
                and not registry_org.is_primed(campaign.org_id)
            ):
                try:
                    global_registry = getattr(registry, "global_registry", None)
                    await registry_org.prime_org_registry(
                        session, settings, campaign.org_id, global_registry=global_registry
                    )
                except Exception:  # noqa: BLE001 - priming must not kill the whole tick
                    log.exception("org_registry_prime_failed", org_id=str(campaign.org_id))
        finally:
            registry_org.CURRENT_ORG_ID.reset(org_token)

        if campaign.status == "scheduled":
            campaign.status = "running"
            await session.commit()

        if campaign.dialer_mode == "preview":
            # DR-10: preview mode is agent-driven, one explicit launch at a time - the
            # tick never auto-dials it.
            continue

        lines = pacing.parallel_lines_allowed(
            campaign.dialer_mode or "power", campaign.parallel_lines
        )
        if campaign.dialer_mode == "predictive":
            coefficient = await _predictive_coefficient(session, campaign.id)
            lines = max(1, round(lines * coefficient))
        lines = min(lines, remaining)

        rows = await _claim_due_rows(session, campaign.id, moment, lines)
        if not rows:
            if not await _has_pending_dial_rows(session, campaign.id):
                campaign.status = "completed"
                # 6.15: voice campaigns never emitted this - the SMS side already does
                # (services/outbound.py) via the exact same durable-outbox primitive.
                record_platform_event(
                    session,
                    campaign.org_id,
                    "campaign.completed",
                    {"campaign_id": str(campaign.id), "name": campaign.name, "channel": "voice"},
                )
                await session.commit()
                counts["completed_campaigns"] += 1
            continue

        numbers = list(
            (await session.execute(sa.select(OrgNumber).where(OrgNumber.is_active.is_(True))))
            .scalars()
            .all()
        )
        warmup_by_number = {n.e164: _aware(n.warmup_started_at) for n in numbers}

        eligible: list[DialAttempt] = []
        for row in rows:
            allowed, defer_until = await _compliance_precheck(
                session, row.e164, campaign.org_id, moment
            )
            if not allowed and defer_until is not None:
                row.next_attempt_at = defer_until
                counts["deferred"] += 1
                await session.commit()
                continue
            if not allowed:
                # Permanent compliance block (opted-out/DNC): never retried. DIAL_STATUSES
                # has no dedicated "blocked" value (models/outbound.py is schema-frozen for
                # this phase, per phase-11-plan's allowed-files list) - "failed" is the
                # closest terminal status, and this row's attempts/next_attempt_at both
                # stay at their defaults, so nothing ever re-queues it. `disposition` still
                # distinguishes this from a genuine carrier/no-answer failure.
                row.status = "failed"
                row.disposition = "blocked"
                await session.commit()
                continue
            from_e164 = _dial_from_number(campaign, numbers, row.e164)
            if from_e164 is None:
                continue  # nothing to dial from; leave the row queued for a later tick

            # 6.5: apply the SAME pacing gate (daily cap + warmup ramp + rate_per_minute)
            # the SMS outbound tick applies - dialer_tick previously ignored all three,
            # so a voice campaign could blow through a brand-new number's warmup ramp or
            # a configured daily_cap/rate_per_minute entirely.
            state = pace_cache.get((campaign.org_id, from_e164))
            if state is None:
                since = moment - timedelta(hours=CAP_WINDOW_HOURS)
                state = {
                    "last_send_at": await _last_dial_at(session, campaign.org_id, from_e164),
                    "sent_today": await _dial_recent_count(
                        session, campaign.org_id, from_e164, since
                    ),
                }
                pace_cache[(campaign.org_id, from_e164)] = state

            ramp_cap = pacing.warmup_daily_cap(warmup_by_number.get(from_e164), moment)
            cap = pacing.effective_daily_cap(campaign.daily_cap, ramp_cap, campaign.respect_warmup)
            if state["sent_today"] >= cap:
                continue  # this number is capped for today; leave the row for a later tick

            due = pacing.next_send_due(state["last_send_at"], campaign.rate_per_minute, rng)
            if due is not None and due > moment:
                continue  # not this number's turn yet

            # NOTE: state["last_send_at"]/["sent_today"] are deliberately NOT updated
            # here. Parallel/predictive modes dial several rows from the SAME number
            # simultaneously in one wave (asyncio.gather below) - mutating the shared
            # pace_cache entry per-row while still collecting that same wave would gate
            # sibling rows against each other and serialize what must go out together.
            # They are updated once per row in the outcome loop below instead.
            row.status = "dialing"
            # Transient, not a mapped column - same discipline as
            # messaging._dispatch_to_carrier's last_carrier_error attribute.
            row._dial_from_e164 = from_e164
            await session.commit()
            eligible.append(row)

        if eligible:
            # DR-13: the injectable dial seam. Existing voice tests replace _start_call
            # with a fake whose signature has no agent_profile_id keyword - pass it only
            # for an ai_calls campaign so plain voice campaigns are unaffected.
            assistant_kwargs = (
                {"agent_profile_id": campaign.agent_profile_id}
                if campaign.channel == "ai_calls"
                else {}
            )
            outcomes = await asyncio.gather(
                *[
                    _start_call(
                        session,
                        settings,
                        bus,
                        api,
                        org_id=campaign.org_id,
                        to_e164=row.e164,
                        from_e164=row._dial_from_e164,
                        identity=f"dialer-{row.id}",
                        **assistant_kwargs,
                    )
                    for row in eligible
                ]
            )

            winner_index = next(
                (
                    i
                    for i, outcome in enumerate(outcomes)
                    if outcome.status == "connected" and outcome.amd_verdict != "machine"
                ),
                None,
            )
            for i, (row, outcome) in enumerate(zip(eligible, outcomes, strict=True)):
                key = _apply_outcome(row, outcome, campaign, moment)
                if key == "connected" and winner_index is not None and i != winner_index:
                    # DR-10 (parallel mode): the first connect wins. The losing leg is a
                    # LIVE human on an open line, not just a row to relabel - it must
                    # actually be hung up, or the sibling is left connected to a dead call.
                    if api is not None and outcome.call_id is not None:
                        call = await session.get(Call, outcome.call_id)
                        if call is not None:
                            await voice_plane_svc.end_room_call(api, call)
                    # 6.21: an abandoned parallel-dial leg is a contact who WAS reached
                    # (a live line, just not the one that won) - it must be retried like
                    # any other non-terminal outcome, never left dangling with no
                    # next_attempt_at and thus no path back into a future tick's claim.
                    if row.attempts < campaign.max_attempts:
                        row.status = "queued"
                        row.next_attempt_at = moment + timedelta(
                            minutes=campaign.retry_backoff_minutes
                        )
                    else:
                        row.status = "abandoned"
                    key = "abandoned"
                if key != "retry_scheduled":
                    counts[key] = counts.get(key, 0) + 1
                # 6.5: advance THIS row's own number's pacing state now that its outcome
                # is known - every dialed row consumes daily volume and advances the
                # rate clock, regardless of parallel/sequential mode.
                state = pace_cache.get((campaign.org_id, row._dial_from_e164))
                if state is not None:
                    state["last_send_at"] = moment
                    state["sent_today"] += 1
                await session.commit()

        remaining -= len(rows)

    return counts
