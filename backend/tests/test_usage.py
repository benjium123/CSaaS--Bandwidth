"""P13 DR-2: usage rollup derives sms_segments/mms_messages/voice_minutes/ai_sms_turns/
ai_tokens/storage_bytes; a re-run REPLACES, never doubles; reconciliation is THE GATE."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    AgentSmsTurn,
    Call,
    CallRecording,
    MediaAsset,
    Message,
    UsageRecord,
)
from app.services import messaging as messaging_svc
from app.services import usage as usage_svc
from tests.conftest import create_org, register_and_login

OUR = "+12145550100"
CONTACT = "+19725550101"
DAY = date(2026, 6, 15)
DAY_START = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


async def _org_id(client, email: str, name: str) -> uuid.UUID:
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    return uuid.UUID(org["id"])


async def _thread_id(session, org_id: uuid.UUID) -> uuid.UUID:
    set_org_context(session, org_id)
    thread = await messaging_svc.upsert_thread(session, org_id, OUR, CONTACT)
    await session.commit()
    return thread.id


def _outbound_message(
    org_id, thread_id, *, est, carrier, media=None, status="delivered", at=DAY_START
) -> Message:
    return Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread_id,
        direction="outbound",
        status=status,
        from_e164=OUR,
        to_e164=CONTACT,
        body="hi",
        media=media or [],
        segment_count_est=est,
        segment_count_carrier=carrier,
        created_at=at,
    )


async def test_rollup_billing_quantity_prefers_carrier_over_estimate_per_message(
    client, session
):
    """Opus review B3: the BILLED quantity per message is the carrier-reported count
    when the carrier has reported one, else our estimate - not a pure estimate total."""
    org_id = await _org_id(client, "us1@example.com", "Org US1")
    thread_id = await _thread_id(session, org_id)
    set_org_context(session, org_id)
    # Carrier reports 3 segments even though we estimated 1 - billing must use 3.
    session.add(_outbound_message(org_id, thread_id, est=1, carrier=3))
    # No carrier report yet - billing falls back to our estimate of 2.
    session.add(
        _outbound_message(
            org_id, thread_id, est=2, carrier=None, media=[{"asset_id": "a1"}]
        )
    )
    await session.commit()

    counts = await usage_svc.rollup_day(session, org_id, DAY)
    assert counts["orgs"] == 1
    assert counts["metrics_written"] == len(usage_svc.USAGE_METRICS)

    set_org_context(session, org_id)
    rows = {
        r.metric: r
        for r in (
            await session.execute(
                sa.select(UsageRecord).where(
                    UsageRecord.org_id == org_id, UsageRecord.period_date == DAY
                )
            )
        )
        .scalars()
        .all()
    }
    assert rows["sms_segments"].quantity == 5  # 3 (carrier) + 2 (estimate) = 5, not 1+2
    assert rows["sms_segments"].carrier_quantity == 3  # sum of carrier-reported rows only
    assert rows["mms_messages"].quantity == 1
    assert rows["mms_messages"].carrier_quantity is None


async def test_rollup_excludes_queued_and_rejected_messages_from_billing(client, session):
    """Opus review item 9: a carrier never bills a message it never accepted."""
    org_id = await _org_id(client, "us1b@example.com", "Org US1B")
    thread_id = await _thread_id(session, org_id)
    set_org_context(session, org_id)
    session.add(_outbound_message(org_id, thread_id, est=1, carrier=1, status="delivered"))
    session.add(_outbound_message(org_id, thread_id, est=5, carrier=None, status="queued"))
    session.add(_outbound_message(org_id, thread_id, est=7, carrier=None, status="rejected"))
    await session.commit()

    await usage_svc.rollup_day(session, org_id, DAY)

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(UsageRecord).where(
                UsageRecord.org_id == org_id,
                UsageRecord.period_date == DAY,
                UsageRecord.metric == "sms_segments",
            )
        )
    ).scalar_one()
    assert row.quantity == 1  # only the delivered message counts, not 1+5+7


async def test_rollup_derives_voice_minutes_ceil_per_call(client, session):
    org_id = await _org_id(client, "us2@example.com", "Org US2")
    set_org_context(session, org_id)
    session.add(
        Call(
            id=uuid.uuid4(),
            org_id=org_id,
            direction="outbound",
            contact_e164=CONTACT,
            our_e164=OUR,
            carrier="bandwidth",
            status="completed",
            duration_seconds=65,  # ceil(65/60) = 2
            ended_at=DAY_START,
            created_at=DAY_START,
        )
    )
    session.add(
        Call(
            id=uuid.uuid4(),
            org_id=org_id,
            direction="inbound",
            contact_e164=CONTACT,
            our_e164=OUR,
            carrier="bandwidth",
            status="completed",
            duration_seconds=30,  # ceil(30/60) = 1
            ended_at=DAY_START,
            created_at=DAY_START,
        )
    )
    await session.commit()

    await usage_svc.rollup_day(session, org_id, DAY)

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(UsageRecord).where(
                UsageRecord.org_id == org_id,
                UsageRecord.period_date == DAY,
                UsageRecord.metric == "voice_minutes",
            )
        )
    ).scalar_one()
    assert row.quantity == 3
    assert row.carrier_quantity is None


async def test_rollup_derives_ai_sms_turns_and_tokens(client, session):
    org_id = await _org_id(client, "us3@example.com", "Org US3")
    thread_id = await _thread_id(session, org_id)
    set_org_context(session, org_id)
    inbound = Message(
        id=uuid.uuid4(),
        org_id=org_id,
        thread_id=thread_id,
        direction="inbound",
        status="received",
        from_e164=CONTACT,
        to_e164=OUR,
        body="hi",
        created_at=DAY_START,
    )
    session.add(inbound)
    await session.flush()
    session.add(
        AgentSmsTurn(
            id=uuid.uuid4(),
            org_id=org_id,
            thread_id=thread_id,
            inbound_message_id=inbound.id,
            status="replied",
            detail="",
            tokens_in=100,
            tokens_out=50,
            created_at=DAY_START,
        )
    )
    await session.commit()

    await usage_svc.rollup_day(session, org_id, DAY)

    set_org_context(session, org_id)
    rows = {
        r.metric: r
        for r in (
            await session.execute(
                sa.select(UsageRecord).where(
                    UsageRecord.org_id == org_id, UsageRecord.period_date == DAY
                )
            )
        )
        .scalars()
        .all()
    }
    assert rows["ai_sms_turns"].quantity == 1
    assert rows["ai_tokens"].quantity == 150


async def test_rollup_derives_storage_bytes_snapshot(client, session):
    org_id = await _org_id(client, "us4@example.com", "Org US4")
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="outbound",
        contact_e164=CONTACT,
        our_e164=OUR,
        carrier="bandwidth",
        status="completed",
    )
    session.add(call)
    await session.flush()
    session.add(
        MediaAsset(
            id=uuid.uuid4(),
            org_id=org_id,
            direction="outbound",
            storage_key="k1",
            status="stored",
            size_bytes=100,
        )
    )
    session.add(
        CallRecording(
            id=uuid.uuid4(),
            org_id=org_id,
            call_id=call.id,
            provider_recording_id="rec-1",
            storage_key="rk1",
            status="stored",
            size_bytes=50,
        )
    )
    await session.commit()

    await usage_svc.rollup_day(session, org_id, DAY)

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(UsageRecord).where(
                UsageRecord.org_id == org_id,
                UsageRecord.period_date == DAY,
                UsageRecord.metric == "storage_bytes",
            )
        )
    ).scalar_one()
    assert row.quantity == 150


async def test_rerolling_an_old_day_does_not_touch_storage_bytes(client, session):
    """Opus review B4: storage_bytes is a live SNAPSHOT, not a per-day delta - re-rolling
    a PAST day (a backfill, include_snapshot=False) must leave its already-written
    storage_bytes figure exactly as it was, never overwrite it with today's live total."""
    org_id = await _org_id(client, "us4b@example.com", "Org US4B")
    set_org_context(session, org_id)
    session.add(
        MediaAsset(
            id=uuid.uuid4(), org_id=org_id, direction="outbound", storage_key="k1",
            status="stored", size_bytes=100,
        )
    )
    await session.commit()

    # First roll of DAY as "today" (default include_snapshot=True) captures the snapshot.
    await usage_svc.rollup_day(session, org_id, DAY)
    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(UsageRecord).where(
                UsageRecord.org_id == org_id,
                UsageRecord.period_date == DAY,
                UsageRecord.metric == "storage_bytes",
            )
        )
    ).scalar_one()
    assert row.quantity == 100

    # Storage grows a lot AFTER that snapshot (simulating today's live growth).
    set_org_context(session, org_id)
    session.add(
        MediaAsset(
            id=uuid.uuid4(), org_id=org_id, direction="outbound", storage_key="k2",
            status="stored", size_bytes=900,
        )
    )
    await session.commit()

    # Re-rolling DAY as a BACKFILL (include_snapshot=False) must not touch storage_bytes.
    await usage_svc.rollup_day(session, org_id, DAY, include_snapshot=False)
    set_org_context(session, org_id)
    await session.refresh(row)
    assert row.quantity == 100  # unchanged, not 1000

    # A day that NEVER had a snapshot written gets no storage_bytes row at all - "we
    # don't know" beats a confidently wrong zero.
    other_day = DAY - timedelta(days=3)
    await usage_svc.rollup_day(session, org_id, other_day, include_snapshot=False)
    set_org_context(session, org_id)
    missing = (
        await session.execute(
            sa.select(UsageRecord).where(
                UsageRecord.org_id == org_id,
                UsageRecord.period_date == other_day,
                UsageRecord.metric == "storage_bytes",
            )
        )
    ).scalar_one_or_none()
    assert missing is None


async def test_rerunning_a_day_replaces_never_doubles(client, session):
    org_id = await _org_id(client, "us5@example.com", "Org US5")
    thread_id = await _thread_id(session, org_id)
    set_org_context(session, org_id)
    session.add(_outbound_message(org_id, thread_id, est=1, carrier=1))
    await session.commit()

    await usage_svc.rollup_day(session, org_id, DAY)
    await usage_svc.rollup_day(session, org_id, DAY)

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(UsageRecord).where(
                UsageRecord.org_id == org_id,
                UsageRecord.period_date == DAY,
                UsageRecord.metric == "sms_segments",
            )
        )
    ).scalar_one()
    assert row.quantity == 1  # not 2, despite two rollup passes

    set_org_context(session, org_id)
    session.add(_outbound_message(org_id, thread_id, est=4, carrier=4))
    await session.commit()
    await usage_svc.rollup_day(session, org_id, DAY)

    set_org_context(session, org_id)
    await session.refresh(row)
    assert row.quantity == 5  # replaced with the freshly-derived total, not 1+5


async def test_reconciliation_reports_estimate_vs_carrier_and_verdict(client, session):
    """THE GATE (DR-2), Opus review B3: reconciliation must be like-for-like. One message
    has BOTH sides (est=10/carrier=10) - fully reconciled, within tolerance. A SECOND
    message has only our estimate (carrier=None, still awaiting its DLR) - that one must
    show up as `pending_dlrs`, NEVER get folded into `ours` and manufacture a false
    mismatch against a carrier figure that doesn't exist yet."""
    org_id = await _org_id(client, "us6@example.com", "Org US6")
    thread_id = await _thread_id(session, org_id)
    set_org_context(session, org_id)
    session.add(_outbound_message(org_id, thread_id, est=10, carrier=10))
    session.add(_outbound_message(org_id, thread_id, est=10, carrier=None))
    await session.commit()
    await usage_svc.rollup_day(session, org_id, DAY)

    items = {i["metric"]: i for i in await usage_svc.reconciliation(session, org_id, DAY)}
    sms = items["sms_segments"]
    assert sms["ours"] == 10  # only the RECONCILED message's estimate, not 10+10
    assert sms["carrier"] == 10
    assert sms["delta"] == 0
    assert sms["within_tolerance"] is True
    assert sms["verdict"] == "within_tolerance"
    assert sms["pending_dlrs"] == 1  # the still-in-flight message, reported separately

    # A metric with no carrier side is honestly not_applicable, never a fake match.
    voice = items["voice_minutes"]
    assert voice["carrier"] is None
    assert voice["within_tolerance"] is None
    assert voice["verdict"] == "not_applicable"
    assert voice["pending_dlrs"] == 0


async def test_reconciliation_flags_a_mismatch_outside_tolerance(client, session):
    org_id = await _org_id(client, "us7@example.com", "Org US7")
    thread_id = await _thread_id(session, org_id)
    set_org_context(session, org_id)
    # est=10 (ours), carrier reports 20 - a 100% delta, well outside 5% tolerance.
    session.add(_outbound_message(org_id, thread_id, est=10, carrier=20))
    await session.commit()
    await usage_svc.rollup_day(session, org_id, DAY)

    items = {i["metric"]: i for i in await usage_svc.reconciliation(session, org_id, DAY)}
    sms = items["sms_segments"]
    assert sms["ours"] == 10
    assert sms["carrier"] == 20
    assert sms["within_tolerance"] is False
    assert sms["verdict"] == "mismatch"
    assert sms["pending_dlrs"] == 0  # the only message IS reconciled - it's just wrong


async def test_reconciliation_all_pending_reports_not_applicable(client, session):
    """Opus review B3: when NOTHING has a carrier report yet, there is nothing to
    reconcile - the verdict must be honestly not_applicable, not a manufactured match."""
    org_id = await _org_id(client, "us7b@example.com", "Org US7B")
    thread_id = await _thread_id(session, org_id)
    set_org_context(session, org_id)
    session.add(_outbound_message(org_id, thread_id, est=10, carrier=None))
    await session.commit()
    await usage_svc.rollup_day(session, org_id, DAY)

    items = {i["metric"]: i for i in await usage_svc.reconciliation(session, org_id, DAY)}
    sms = items["sms_segments"]
    assert sms["carrier"] is None
    assert sms["within_tolerance"] is None
    assert sms["verdict"] == "not_applicable"
    assert sms["pending_dlrs"] == 1


async def test_usage_tick_rolls_up_yesterday_and_today(client, session):
    org_id = await _org_id(client, "us8@example.com", "Org US8")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    counts = await usage_svc.usage_tick(session, now=now)
    assert counts["orgs"] == 2  # one org, rolled up twice (yesterday + today)

    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(UsageRecord.period_date).where(UsageRecord.org_id == org_id).distinct()
        )
    ).scalars().all()
    assert set(rows) == {now.date(), now.date() - timedelta(days=1)}
