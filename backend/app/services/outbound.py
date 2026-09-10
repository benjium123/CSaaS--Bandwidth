"""SMS campaign lifecycle and the sweeper tick (phase-11-plan DR-2..DR-9, DR-14).

DR-2: every campaign SMS goes through ``services.messaging.send_message`` and therefore
the full compliance gate - this module never re-implements opt-out/DNC/quiet-hours. It may
pre-filter known-DNC rows at IMPORT time (``services.list_import``) to save work, but the
gate remains the sole authority at send time.

DR-3: campaign sends set ``session.info[BULK_SEND_KEY] = True`` around the
``send_message`` call so a bulk send is never mistaken for a human takeover of an active AI
thread (mirrors ``AI_SEND_KEY``).

DR-4: a row is claimed (``queued`` -> ``sending``) and committed BEFORE ``send_message`` is
called, and the final outcome is written in its own commit right after - so a crash between
those two commits leaves the row in ``sending`` with no ``message_id``, which the next
tick's staleness sweep re-queues. ``outbound_sends`` is UNIQUE on (campaign_id, e164), so
enqueueing a contact twice is impossible at the database.

DR-7: pacing is per sending number, with +/-20% jitter (``services.pacing``), and a tick
sends at most ``OUTBOUND_TICK_BATCH`` rows total.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from random import Random

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.compliance import registration
from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import (
    CarrierNotConfiguredError,
    ComplianceBlockedError,
    ConflictError,
    ValidationFailedError,
)
from app.models import (
    SEND_TERMINAL,
    ContactList,
    ContactListRow,
    Message,
    Org,
    OrgNumber,
    OutboundCampaign,
    OutboundSend,
)
from app.providers import registry_org
from app.routing import router as routing
from app.services import credentials as credential_svc
from app.services import pacing, smart_routing
from app.services import sender as sender_svc
from app.services import templates as tmpl
from app.services.messaging import BULK_SEND_KEY, send_message
from app.services.outbox import record_platform_event

log = structlog.get_logger("outbound")

OUTBOUND_TICK_BATCH = 25
STALE_SENDING_MINUTES = 5
#: D2: when a campaign's entire number pool is registration-ineligible right now, a
#: queued row is left alone rather than blocked - but bumped this far out so it is not
#: immediately re-selected (and re-checked) on every subsequent tick until that changes.
NO_ELIGIBLE_SENDER_RETRY_MINUTES = 5
#: 6.16: enqueue_campaign_rows commits (and frees its identity map) every this many rows,
#: instead of holding an entire campaign's worth of new OutboundSend rows in one txn.
ENQUEUE_BATCH = 500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_sqlite(session: AsyncSession) -> bool:
    return session.get_bind().dialect.name == "sqlite"


def _bind(session: AsyncSession, moment: datetime) -> datetime:
    return moment.replace(tzinfo=None) if _is_sqlite(session) else moment


#: Daily-cap window. TRAILING, not calendar-midnight: a rolling window has no midnight
#: burst, matches what carrier warm-up actually cares about (recent volume), and keeps
#: the derived count correct when the tick clock is test-frozen ahead of DB-real
#: created_at timestamps. 26h (not 24h) leaves margin for the frozen clock sitting up to
#: a day ahead of row timestamps - slightly conservative, never over-sending.
CAP_WINDOW_HOURS = 26


# --------------------------------------------------------------------------------------
# Campaign lifecycle
# --------------------------------------------------------------------------------------
async def create_campaign(session: AsyncSession, org_id: uuid.UUID, **fields) -> OutboundCampaign:
    campaign = OutboundCampaign(id=uuid.uuid4(), org_id=org_id, **fields)
    session.add(campaign)
    await session.commit()
    return campaign


async def _list_has_per_row_messages(session: AsyncSession, list_id: uuid.UUID) -> bool:
    stmt = sa.select(ContactListRow.fields).where(
        ContactListRow.list_id == list_id, ContactListRow.status == "accepted"
    )
    rows = (await session.execute(stmt)).scalars().all()
    return any((fields or {}).get("message", "").strip() for fields in rows)


async def enqueue_campaign_rows(session: AsyncSession, campaign: OutboundCampaign) -> int:
    """Enqueue every ACCEPTED row of the campaign's list as an ``outbound_sends`` row.
    Naturally idempotent across repeated calls - UNIQUE(campaign_id, e164) makes a
    double-enqueue impossible at the database (DR-4)."""
    existing = set(
        (
            await session.execute(
                sa.select(OutboundSend.e164).where(OutboundSend.campaign_id == campaign.id)
            )
        )
        .scalars()
        .all()
    )
    # 6.16: select only the three columns actually needed (not full ORM rows) and commit
    # + expunge every ENQUEUE_BATCH rows - a 100k-row list must not hold every
    # ContactListRow AND every newly-built OutboundSend in memory for one giant
    # transaction/identity map.
    stmt = sa.select(ContactListRow.id, ContactListRow.contact_id, ContactListRow.e164).where(
        ContactListRow.list_id == campaign.list_id, ContactListRow.status == "accepted"
    )
    created = 0
    since_commit = 0
    for row_id, contact_id, e164 in (await session.execute(stmt)).all():
        if e164 in existing:
            continue
        session.add(
            OutboundSend(
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


async def start_campaign(session: AsyncSession, campaign: OutboundCampaign) -> OutboundCampaign:
    """Start an SMS campaign (channel="sms"). Voice campaigns start through
    ``services.dialer.start_dial_campaign`` - the route layer dispatches on channel."""
    if campaign.channel != "sms":
        raise ValidationFailedError("start_campaign is only for channel='sms' campaigns")
    if campaign.status not in ("draft", "scheduled", "paused"):
        raise ConflictError(f"Cannot start a campaign in status {campaign.status!r}")
    contact_list = await session.get(ContactList, campaign.list_id)
    if contact_list is None or contact_list.status != "ready":
        raise ValidationFailedError("The campaign's list is not ready yet")
    # DR-14: startable when there is a campaign body OR the list carries per-row messages.
    if not (campaign.body or "").strip() and not await _list_has_per_row_messages(
        session, campaign.list_id
    ):
        raise ValidationFailedError(
            "This campaign has no message body, and its list has no per-row messages"
        )
    await enqueue_campaign_rows(session, campaign)
    # 6.6: start_at was dead - /start always ran the campaign immediately regardless of
    # a future start_at. A future start_at now parks the campaign as "scheduled";
    # outbound_tick releases it into "running" once that moment has passed.
    start_at = campaign.start_at
    if start_at is not None and start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=timezone.utc)
    campaign.status = "scheduled" if (start_at is not None and start_at > _now()) else "running"
    await session.commit()
    return campaign


async def pause_campaign(session: AsyncSession, campaign: OutboundCampaign) -> OutboundCampaign:
    """Channel-agnostic: used for both sms and voice campaigns."""
    if campaign.status != "running":
        raise ConflictError(f"Cannot pause a campaign in status {campaign.status!r}")
    campaign.status = "paused"
    await session.commit()
    return campaign


async def cancel_campaign(session: AsyncSession, campaign: OutboundCampaign) -> OutboundCampaign:
    """Channel-agnostic: used for both sms and voice campaigns."""
    if campaign.status in ("completed", "cancelled"):
        raise ConflictError(f"Cannot cancel a campaign in status {campaign.status!r}")
    campaign.status = "cancelled"
    await session.commit()
    return campaign


# --------------------------------------------------------------------------------------
# Body resolution (DR-14)
# --------------------------------------------------------------------------------------
def _resolve_body(
    campaign: OutboundCampaign, row: OutboundSend | None, fields: dict, org_name: str
) -> tuple[str | None, str | None]:
    """Returns (body, skip_reason). ``skip_reason`` is set (and body is None) only when
    there is truly nothing to send."""
    if (campaign.body or "").strip():
        namespace = {"contact": dict(fields or {}), "org": {"name": org_name}}
        try:
            result = tmpl.render(campaign.body, namespace)
            return result.body, None
        except tmpl.UnknownTokenError:
            # A campaign body referencing a merge field this particular row does not have
            # is treated exactly like an empty body (DR-14's fallback chain) rather than
            # failing the whole send outright.
            pass
    message = ((fields or {}).get("message") or "").strip()
    if message:
        return message, None
    return None, "no message"


# --------------------------------------------------------------------------------------
# The sweeper tick
# --------------------------------------------------------------------------------------
async def _running_sms_campaigns(
    session: AsyncSession, now: datetime
) -> list[OutboundCampaign]:
    stmt = (
        sa.select(OutboundCampaign)
        .where(
            OutboundCampaign.channel == "sms",
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


#: How far before a stale row's own claim timestamp to look for a Message that might be
#: the send it was in the middle of - covers the gap between the claim-commit and
#: send_message's own commit of the Message row, not a real search window.
_ADOPT_SEARCH_SLACK = timedelta(seconds=30)


async def _requeue_stale_sending(session: AsyncSession, now: datetime) -> int:
    """DR-4 crash recovery. A ``sending`` row with no ``message_id``, untouched for
    STALE_SENDING_MINUTES, means the tick that claimed it may have crashed somewhere
    between the claim-commit and the outcome-commit. ``send_message`` commits the Message
    row on its OWN, before this scheduler ever links it back - so blindly flipping the row
    to ``queued`` again risks a genuine double-send on the next tick. Before doing that,
    look for a Message that landed for this exact contact around when the row was claimed
    and ADOPT it instead. Only a row with no such evidence is actually re-queued, and
    re-queues are themselves capped by the campaign's own ``max_attempts`` so a row that
    keeps crashing terminates instead of looping forever.
    """
    cutoff = _bind(session, now - timedelta(minutes=STALE_SENDING_MINUTES))
    stmt = (
        sa.select(OutboundSend)
        .where(
            OutboundSend.status == "sending",
            OutboundSend.message_id.is_(None),
            OutboundSend.updated_at < cutoff,
        )
        .execution_options(**{ALLOW_UNSCOPED_KEY: True})
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for row in rows:
        set_org_context(session, row.org_id)

        # 6.1/2.8: bound the adoption window to THIS row's own org/from/to/body, not just
        # "any outbound message to this contact around this time" - an unbounded search
        # could adopt an unrelated message (different campaign, different body, even a
        # 1:1 reply) that merely happened to land in the same few seconds.
        campaign = await session.get(OutboundCampaign, row.campaign_id)
        row_fields = {}
        if row.row_id is not None:
            list_row = await session.get(ContactListRow, row.row_id)
            row_fields = (list_row.fields if list_row is not None else {}) or {}
        expected_body, _skip = _resolve_body(campaign, row, row_fields, "")
        if expected_body is None:
            row.attempts += 1
            if campaign is not None and row.attempts >= campaign.max_attempts:
                row.status = "failed"
                row.last_error = "requeue attempts exhausted after a suspected crash"
            else:
                row.status = "queued"
            await session.commit()
            continue

        adopt_window = _bind(session, now - timedelta(minutes=15))
        search_from = _bind(session, row.updated_at - _ADOPT_SEARCH_SLACK)
        if search_from < adopt_window:
            search_from = adopt_window

        from_e164 = None
        if campaign is not None:
            numbers = list(
                (
                    await session.execute(
                        sa.select(OrgNumber).where(OrgNumber.is_active.is_(True))
                    )
                )
                .scalars()
                .all()
            )
            pool = [
                n.e164
                for n in numbers
                if not campaign.from_numbers or n.e164 in campaign.from_numbers
            ]
            if pool:
                from_e164 = sender_svc.pick_deterministic(row.e164, pool)

        adopted_stmt = (
            sa.select(Message)
            .where(
                Message.org_id == row.org_id,
                Message.to_e164 == row.e164,
                Message.direction == "outbound",
                Message.body == expected_body,
                Message.created_at >= search_from,
                Message.created_at <= _bind(session, now),
            )
            .order_by(Message.created_at.asc())
            .limit(1)
        )
        if from_e164:
            adopted_stmt = adopted_stmt.where(Message.from_e164 == from_e164)

        adopted = (await session.execute(adopted_stmt)).scalar_one_or_none()

        if adopted is not None:
            row.message_id = adopted.id
            if adopted.status in ("rejected", "failed"):
                # 6.1: an adopted message that the carrier actually REJECTED must never
                # be reported as "sent" - the recipient never got it.
                row.status = "failed"
                row.last_error = (adopted.error_detail or adopted.error_code or "adopted_failed")[
                    :255
                ]
            else:
                row.status = "deferred" if adopted.hold_until is not None else "sent"
            await session.commit()
            continue

        row.attempts += 1
        if campaign is not None and row.attempts >= campaign.max_attempts:
            row.status = "failed"
            row.last_error = "requeue attempts exhausted after a suspected crash"
        else:
            row.status = "queued"
        await session.commit()
    return len(rows)


async def _has_nonterminal_sends(session: AsyncSession, campaign_id: uuid.UUID) -> bool:
    stmt = (
        sa.select(OutboundSend.id)
        .where(OutboundSend.campaign_id == campaign_id, OutboundSend.status.notin_(SEND_TERMINAL))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def _sent_recent_count(session: AsyncSession, from_e164: str, since: datetime) -> int:
    stmt = sa.select(sa.func.count()).select_from(Message).where(
        Message.from_e164 == from_e164,
        Message.direction == "outbound",
        Message.created_at >= _bind(session, since),
    )
    return (await session.execute(stmt)).scalar_one()


async def _last_send_at(session: AsyncSession, from_e164: str) -> datetime | None:
    stmt = sa.select(sa.func.max(Message.created_at)).where(
        Message.from_e164 == from_e164, Message.direction == "outbound"
    )
    value = (await session.execute(stmt)).scalar_one_or_none()
    if value is not None and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _aware(value: datetime | None) -> datetime | None:
    """SQLite round-trips DateTime(timezone=True) as naive; ``pacing.warmup_daily_cap``
    compares its ``warmup_started_at`` argument against an aware ``now``, so a naive value
    read back from the ORM must be normalized before it ever reaches that comparison."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def outbound_tick(
    session: AsyncSession,
    carrier,  # noqa: ANN001 - MessagingCarrier protocol
    settings,  # noqa: ANN001 - app.config.Settings
    rng: Random,
    *,
    now: datetime | None = None,
    batch: int = OUTBOUND_TICK_BATCH,
    registry=None,  # noqa: ANN001 - CarrierRegistry; enables failover when given
) -> dict[str, int]:
    """One pass over every ``running`` SMS campaign. Never raises on a per-row send
    failure - only ``ComplianceBlockedError`` is caught explicitly; anything else
    propagates so the sweeper logs it and the next tick's staleness sweep recovers the row."""
    moment = now or _now()
    counts = {
        "requeued_stale": await _requeue_stale_sending(session, moment),
        "sent": 0,
        "deferred": 0,
        "blocked": 0,
        "failed": 0,
        "skipped": 0,
        "completed_campaigns": 0,
    }
    remaining = batch
    # Per-number pacing state, seeded lazily and kept ACROSS campaigns within this one
    # tick call - two campaigns sharing a sending number must not out-pace each other.
    # 6.20: keyed on (org_id, from_e164) - two different orgs can each legitimately hold
    # the SAME literal from_e164 in test/dev data, and even in prod a stale cross-org
    # collision must never throttle one org off of another org's send history.
    pace_cache: dict[tuple[uuid.UUID, str], dict] = {}

    for campaign in await _running_sms_campaigns(session, moment):
        if remaining <= 0:
            break
        set_org_context(session, campaign.org_id)

        # 4.2: DB-backed org carrier registries are keyed by CURRENT_ORG_ID. Prime that
        # context around this campaign so a carrier provisioned via the org's own P17
        # account is used instead of silently falling back to the env carrier - the
        # sweeper runs with CURRENT_ORG_ID=None otherwise. Mirrors number_orders.py.
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

            # 6.18: resolve the carrier to actually dispatch THROUGH the (now primed,
            # CURRENT_ORG_ID-scoped) registry rather than always the single env `carrier`
            # parameter - a DB-only org (no env carrier configured at all, `carrier is
            # None`) previously could never send a campaign even though outbound_tick
            # runs for it; this is the other half of that fix (the sweeper-level gate
            # change is in sweeper.py).
            campaign_carrier = carrier
            if registry is not None:
                resolved = getattr(registry, "primary", lambda: None)()
                if resolved is not None:
                    campaign_carrier = resolved
        finally:
            registry_org.CURRENT_ORG_ID.reset(org_token)

        if campaign_carrier is None:
            # Nothing this org can send through at all - leave its rows queued for a
            # later tick rather than crashing on a None carrier.
            continue

        if campaign.status == "scheduled":
            campaign.status = "running"
            await session.commit()

        org = await session.get(
            Org, campaign.org_id, execution_options={ALLOW_UNSCOPED_KEY: True}
        )
        org_name = org.name if org is not None else ""

        numbers = list(
            (await session.execute(sa.select(OrgNumber).where(OrgNumber.is_active.is_(True))))
            .scalars()
            .all()
        )
        # D2: filter the pool through the SAME registration-eligibility gate
        # send_message enforces (D1) - selecting an ineligible number here only to have
        # it rejected by send_message's compliance check a few lines below would mark
        # the row "blocked" from a single attempt, with max_attempts/backoff never
        # given a chance to apply.
        eligible_numbers, _ineligible = await registration.partition_by_eligibility(
            session,
            numbers,
            require_registration=bool(
                settings is not None and settings.require_number_registration
            ),
        )
        eligible_e164s = {n.e164 for n in eligible_numbers}
        # D43: a number whose recent sending record is in breach is EXCLUDED from bulk
        # traffic (P21's campaign rule, which until now was unreachable in production
        # because this runner never consulted routing at all). Computed ONCE per campaign
        # per tick - `compute_number_stats` is a trailing-window aggregate, and paying for
        # it on every row would be a real regression. The same set is handed to plan_route
        # below so the runner and the router cannot disagree about which numbers are out.
        try:
            excluded_e164s = await smart_routing.campaign_excluded_e164s(
                session, campaign.org_id
            )
        except Exception:  # noqa: BLE001 - reputation must never kill the whole tick
            log.exception("campaign_reputation_exclusion_failed", campaign_id=str(campaign.id))
            excluded_e164s = set()
        pool = [
            n.e164
            for n in numbers
            if n.e164 in eligible_e164s
            and n.e164 not in excluded_e164s
            and (not campaign.from_numbers or n.e164 in campaign.from_numbers)
        ]
        warmup_by_number = {n.e164: _aware(n.warmup_started_at) for n in numbers}

        stmt = (
            sa.select(OutboundSend)
            .where(
                OutboundSend.campaign_id == campaign.id,
                OutboundSend.status == "queued",
                sa.or_(
                    OutboundSend.next_attempt_at.is_(None),
                    OutboundSend.next_attempt_at <= _bind(session, moment),
                ),
            )
            .order_by(OutboundSend.created_at.asc())
            .limit(remaining)
            # 6.19: SKIP LOCKED so a second concurrent worker claims a DIFFERENT batch of
            # rows instead of blocking on (or double-claiming) this one's. SQLAlchemy
            # silently ignores this on SQLite, so tests keep running unaffected.
            .with_for_update(skip_locked=True)
        )
        rows = list((await session.execute(stmt)).scalars().all())

        for row in rows:
            if remaining <= 0:
                break
            if not pool:
                # D2: every number in campaign.from_numbers is registration-ineligible
                # right now (or none is active) - nothing here will ever succeed until
                # that changes. Leave the row QUEUED, never blocked, but bump
                # next_attempt_at so it is not immediately re-selected on the very next
                # tick, same as the carrier-rejection backoff below.
                row.next_attempt_at = moment + timedelta(
                    minutes=NO_ELIGIBLE_SENDER_RETRY_MINUTES
                )
                await session.commit()
                continue

            # 6.2: go through the SAME sticky-sender selection a reply does, restricted
            # to this campaign's own number pool - raw pick_deterministic bypassed
            # stickiness entirely, forking an existing 1:1 thread onto a different
            # number and splitting its opt-out state. allow_reassign=True: a bulk
            # campaign send should pick another in-pool number rather than blocking the
            # whole row when a contact's prior sticky number fell outside this pool.
            try:
                from_e164 = await sender_svc.select_sender(
                    session, campaign.org_id, row.e164, pool=pool, allow_reassign=True
                )
            except ValidationFailedError:
                # E4: no eligible number for this contact right now (e.g. its sticky
                # number fell outside the pool and no reassignment was possible) - same
                # bump as the empty-pool path above, or this row is re-selected on the
                # very next tick forever (a busy loop), never blocked/failed either.
                row.next_attempt_at = moment + timedelta(
                    minutes=NO_ELIGIBLE_SENDER_RETRY_MINUTES
                )
                await session.commit()
                continue
            state = pace_cache.get((campaign.org_id, from_e164))
            if state is None:
                since = moment - timedelta(hours=CAP_WINDOW_HOURS)
                state = {
                    "last_send_at": await _last_send_at(session, from_e164),
                    "sent_today": await _sent_recent_count(session, from_e164, since),
                }
                pace_cache[(campaign.org_id, from_e164)] = state

            ramp_cap = pacing.warmup_daily_cap(warmup_by_number.get(from_e164), moment)
            cap = pacing.effective_daily_cap(campaign.daily_cap, ramp_cap, campaign.respect_warmup)
            if state["sent_today"] >= cap:
                continue  # this number is capped for today; leave the row for a later tick

            due = pacing.next_send_due(state["last_send_at"], campaign.rate_per_minute, rng)
            if due is not None and due > moment:
                continue  # not this number's turn yet

            row_fields = {}
            if row.row_id is not None:
                list_row = await session.get(ContactListRow, row.row_id)
                row_fields = (list_row.fields if list_row is not None else {}) or {}
            body, skip_reason = _resolve_body(campaign, row, row_fields, org_name)

            row.status = "sending"
            await session.commit()
            remaining -= 1

            if body is None:
                row.status = "skipped"
                row.last_error = skip_reason
                await session.commit()
                counts["skipped"] += 1
                continue

            # D43: build the routing plan for THIS row through the same router a
            # one-to-one send uses, with is_campaign=True so the bulk exclusion rule
            # applies - and hand it to send_message so the customer-facing route sentence
            # lands on `messages.route_reason` exactly the way P21 does for 1:1 sends.
            # `requested_from` pins the number this runner already chose (campaign pool +
            # sticky sender + per-number pacing all hang off it); routing's job here is the
            # carrier, the eligibility gate and the explanation, not re-picking the sender.
            plan = None
            require_reg = bool(
                settings is not None and settings.require_number_registration
            )
            if registry is not None and len(registry) > 0:
                try:
                    plan = await routing.plan_route(
                        session,
                        campaign.org_id,
                        registry,
                        contact_e164=row.e164,
                        requested_from=from_e164,
                        is_campaign=True,
                        campaign_excluded=excluded_e164s,
                        require_registration=require_reg,
                    )
                except ComplianceBlockedError as exc:
                    row.status = "blocked"
                    row.last_error = str(exc)[:255]
                    await session.commit()
                    counts["blocked"] += 1
                    continue
                except (ValidationFailedError, CarrierNotConfiguredError) as exc:
                    # Nothing sendable right now for a reason that may clear on its own
                    # (a number just released, a carrier not yet configured). Same
                    # backoff as the no-eligible-sender path: never a terminal failure.
                    row.status = "queued"
                    row.last_error = str(exc)[:255]
                    row.next_attempt_at = moment + timedelta(
                        minutes=NO_ELIGIBLE_SENDER_RETRY_MINUTES
                    )
                    await session.commit()
                    remaining += 1  # this row was claimed but never dispatched
                    continue

            session.info[BULK_SEND_KEY] = True
            send_org_token = registry_org.CURRENT_ORG_ID.set(campaign.org_id)
            try:
                message = await send_message(
                    session,
                    campaign.org_id,
                    campaign_carrier,
                    to_e164=row.e164,
                    from_e164=from_e164,
                    body=body,
                    registry=registry,
                    plan=plan,
                    # D1: when no plan could be built (no registry at all), this call still
                    # bypasses routing.plan_route - which is otherwise the only place the
                    # 10DLC/TFV registration gate runs - so the gate is passed explicitly.
                    # send_message ignores it when a plan IS present, because the plan's
                    # numbers were already filtered for eligibility while it was built.
                    require_registration=require_reg,
                )
            except ComplianceBlockedError as exc:
                row.status = "blocked"
                row.last_error = str(exc)[:255]
                await session.commit()
                counts["blocked"] += 1
            else:
                if message.hold_until is not None:
                    row.status = "deferred"
                    row.message_id = message.id
                    counts["deferred"] += 1
                elif message.status == "accepted":
                    row.status = "sent"
                    row.message_id = message.id
                    state["last_send_at"] = moment
                    state["sent_today"] += 1
                    counts["sent"] += 1
                else:
                    # Carrier rejection (DR-12): exponential backoff up to max_attempts,
                    # then a terminal failure. Never retried once genuinely exhausted.
                    row.attempts += 1
                    row.message_id = message.id
                    row.last_error = (
                        message.error_detail or message.error_code or "carrier_rejected"
                    )[:255]
                    if row.attempts >= campaign.max_attempts:
                        row.status = "failed"
                        counts["failed"] += 1
                    else:
                        row.status = "queued"
                        row.next_attempt_at = moment + timedelta(
                            minutes=campaign.retry_backoff_minutes * (2 ** (row.attempts - 1))
                        )
                await session.commit()
            finally:
                session.info.pop(BULK_SEND_KEY, None)
                registry_org.CURRENT_ORG_ID.reset(send_org_token)

        if not await _has_nonterminal_sends(session, campaign.id):
            campaign.status = "completed"
            # P13 DR-4: outbox row commits with the completion itself.
            record_platform_event(
                session,
                campaign.org_id,
                "campaign.completed",
                {"campaign_id": str(campaign.id), "name": campaign.name, "channel": "sms"},
            )
            await session.commit()
            counts["completed_campaigns"] += 1

    return counts
