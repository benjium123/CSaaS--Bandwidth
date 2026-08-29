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

from app.db.base import ALLOW_UNSCOPED_KEY, set_org_context
from app.errors import ComplianceBlockedError, ConflictError, ValidationFailedError
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
from app.services import pacing
from app.services import sender as sender_svc
from app.services import templates as tmpl
from app.services.messaging import BULK_SEND_KEY, send_message
from app.services.outbox import record_platform_event

log = structlog.get_logger("outbound")

OUTBOUND_TICK_BATCH = 25
STALE_SENDING_MINUTES = 5


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
    stmt = sa.select(ContactListRow).where(
        ContactListRow.list_id == campaign.list_id, ContactListRow.status == "accepted"
    )
    rows = list((await session.execute(stmt)).scalars().all())
    created = 0
    for row in rows:
        if row.e164 in existing:
            continue
        session.add(
            OutboundSend(
                id=uuid.uuid4(),
                org_id=campaign.org_id,
                campaign_id=campaign.id,
                row_id=row.id,
                contact_id=row.contact_id,
                e164=row.e164,
                status="queued",
            )
        )
        existing.add(row.e164)
        created += 1
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
    campaign.status = "running"
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
async def _running_sms_campaigns(session: AsyncSession) -> list[OutboundCampaign]:
    stmt = (
        sa.select(OutboundCampaign)
        .where(OutboundCampaign.channel == "sms", OutboundCampaign.status == "running")
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

        search_from = _bind(session, row.updated_at - _ADOPT_SEARCH_SLACK)
        adopted = (
            await session.execute(
                sa.select(Message)
                .where(
                    Message.to_e164 == row.e164,
                    Message.direction == "outbound",
                    Message.created_at >= search_from,
                )
                .order_by(Message.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if adopted is not None:
            row.message_id = adopted.id
            row.status = "deferred" if adopted.hold_until is not None else "sent"
            await session.commit()
            continue

        campaign = await session.get(OutboundCampaign, row.campaign_id)
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
    settings,  # noqa: ANN001 - app.config.Settings; reserved for future use
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
    pace_cache: dict[str, dict] = {}

    for campaign in await _running_sms_campaigns(session):
        if remaining <= 0:
            break
        set_org_context(session, campaign.org_id)

        org = await session.get(
            Org, campaign.org_id, execution_options={ALLOW_UNSCOPED_KEY: True}
        )
        org_name = org.name if org is not None else ""

        numbers = list(
            (await session.execute(sa.select(OrgNumber).where(OrgNumber.is_active.is_(True))))
            .scalars()
            .all()
        )
        pool = [
            n.e164
            for n in numbers
            if not campaign.from_numbers or n.e164 in campaign.from_numbers
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
        )
        rows = list((await session.execute(stmt)).scalars().all())

        for row in rows:
            if remaining <= 0:
                break
            if not pool:
                break  # nothing to send from this org right now; leave the row queued

            from_e164 = sender_svc.pick_deterministic(row.e164, pool)
            state = pace_cache.get(from_e164)
            if state is None:
                since = moment - timedelta(hours=CAP_WINDOW_HOURS)
                state = {
                    "last_send_at": await _last_send_at(session, from_e164),
                    "sent_today": await _sent_recent_count(session, from_e164, since),
                }
                pace_cache[from_e164] = state

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

            session.info[BULK_SEND_KEY] = True
            try:
                message = await send_message(
                    session,
                    campaign.org_id,
                    carrier,
                    to_e164=row.e164,
                    from_e164=from_e164,
                    body=body,
                    registry=registry,
                    plan=None,
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
