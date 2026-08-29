"""P11: SMS campaign lifecycle + outbound_tick (services/outbound.py). DR-2..DR-9, DR-14."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from random import Random

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.db.base import set_org_context
from app.errors import ConflictError
from app.models import (
    SEND_TERMINAL,
    Contact,
    ContactList,
    ContactListRow,
    Message,
    MessageThread,
    OrgNumber,
    OutboundCampaign,
    OutboundSend,
)
from app.providers.domain import CarrierError, SendResult
from app.services import list_import as list_import_svc
from app.services import outbound as outbound_svc
from app.services import pacing
from tests.conftest import auth_headers, make_org_with_number

OUR = "+12145550100"
SECOND_NUMBER = "+12145550111"
CONTACT = "+19725550101"

#: A fixed instant, NOT wall-clock time. Used as the base for THE GATE's multi-round
#: drain loop below and for anything else that needs a stable "today" - real wall-clock
#: time would make quiet-hours-adjacent assertions a CI time bomb.
# Deterministic per-run tick clock. The HOUR is pinned at 18:00 UTC because the test
# fixtures depend on that exact relationship (18:00 UTC == 21:00 Moscow == quiet for the
# Moscow-timezone cohort, and == 13:00 CDT == allowed for the US numbers, DST-proof for
# the suite's horizon). The DATE floats: the next 18:00 UTC at least 5 minutes ahead of
# the real wall clock, so pacing never sees a row "sent in the future" and the trailing
# CAP_WINDOW still covers rows stamped with DB-real created_at. A fixed calendar date
# failed both ways (Opus blocker 1, then the 02:00-UTC CDT quiet-hours flip).
_REAL_NOW = datetime.now(timezone.utc)
FROZEN = _REAL_NOW.replace(hour=18, minute=0, second=0, microsecond=0)
if FROZEN < _REAL_NOW + timedelta(minutes=5):
    FROZEN += timedelta(days=1)


async def _make_org(client) -> tuple[str, uuid.UUID]:
    token, org, _ = await make_org_with_number(client, "oc1@example.com", "Org A", OUR)
    return token, uuid.UUID(org["id"])


async def _ready_list(session, org_id: uuid.UUID, entries: list[dict]) -> ContactList:
    """Build a `ready` ContactList with `accepted` rows directly, bypassing the import
    pipeline (that path is covered by test_list_import.py) so these tests stay focused on
    the campaign scheduler."""
    set_org_context(session, org_id)
    lst = ContactList(
        id=uuid.uuid4(), org_id=org_id, name="L", source_filename="l.csv", status="ready",
        total_rows=len(entries), accepted_count=len(entries),
    )
    session.add(lst)
    await session.flush()
    for entry in entries:
        e164 = entry["e164"]
        contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name=e164)
        session.add(contact)
        await session.flush()
        session.add(
            ContactListRow(
                id=uuid.uuid4(), org_id=org_id, list_id=lst.id, row_number=1,
                raw={"phone": e164}, e164=e164, contact_id=contact.id, status="accepted",
                fields=entry.get("fields", {}),
            )
        )
    await session.commit()
    return lst


async def _campaign(session, org_id, list_id, **overrides) -> OutboundCampaign:
    fields = {
        "name": "C", "channel": "sms", "list_id": list_id, "body": "Hello",
        "from_numbers": [OUR], "rate_per_minute": 600, "daily_cap": 200,
        "respect_warmup": False, "max_attempts": 2, "retry_backoff_minutes": 240,
    }
    fields.update(overrides)
    return await outbound_svc.create_campaign(session, org_id, **fields)


# ----------------------------------------------------------------------------------
# Unit: pacing (services/pacing.py) - pure functions, no DB
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "age_days,expected_cap",
    [(1, 50), (3, 50), (7, 100), (14, 250), (30, None)],
)
def test_warmup_daily_cap_ages(age_days, expected_cap):
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = started + timedelta(days=age_days - 1)
    assert pacing.warmup_daily_cap(started, now) == expected_cap


def test_warmup_daily_cap_no_warmup_started_is_uncapped():
    assert pacing.warmup_daily_cap(None, datetime.now(timezone.utc)) is None


def test_effective_daily_cap_respects_warmup_flag():
    # respect_warmup=True: the LOWER of the two always wins.
    assert pacing.effective_daily_cap(200, 50, True) == 50
    # respect_warmup=False: the ramp cap is ignored outright.
    assert pacing.effective_daily_cap(200, 50, False) == 200
    # No ramp cap at all (mature number): the campaign's own cap is all that applies,
    # regardless of the flag.
    assert pacing.effective_daily_cap(200, None, True) == 200
    assert pacing.effective_daily_cap(200, None, False) == 200


def test_send_interval_seconds_bounded_over_seeded_sweep():
    rng = Random(42)
    rate = 6
    base = 60.0 / rate
    for _ in range(2000):
        interval = pacing.send_interval_seconds(rate, rng)
        assert base * 0.8 <= interval <= base * 1.2


# ----------------------------------------------------------------------------------
# Unit: claiming / idempotency
# ----------------------------------------------------------------------------------
async def test_enqueue_is_idempotent(app_with_loopback, session):
    client, _carrier, _app = app_with_loopback
    _token, org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [{"e164": CONTACT}])
    campaign = await _campaign(session, org_id, lst.id)

    first = await outbound_svc.enqueue_campaign_rows(session, campaign)
    second = await outbound_svc.enqueue_campaign_rows(session, campaign)
    assert first == 1
    assert second == 0

    count = (
        await session.execute(
            sa.select(sa.func.count()).select_from(OutboundSend).where(
                OutboundSend.campaign_id == campaign.id
            )
        )
    ).scalar_one()
    assert count == 1


async def test_unique_constraint_blocks_a_duplicate_row_at_the_db(app_with_loopback, session):
    client, _carrier, _app = app_with_loopback
    _token, org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [{"e164": CONTACT}])
    campaign = await _campaign(session, org_id, lst.id)
    await outbound_svc.enqueue_campaign_rows(session, campaign)

    set_org_context(session, org_id)
    session.add(
        OutboundSend(
            id=uuid.uuid4(), org_id=org_id, campaign_id=campaign.id, e164=CONTACT, status="queued"
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_stale_sending_rows_are_requeued(app_with_loopback, session):
    client, carrier, _app = app_with_loopback
    _token, org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [{"e164": CONTACT}])
    campaign = await _campaign(session, org_id, lst.id)
    await outbound_svc.enqueue_campaign_rows(session, campaign)

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(OutboundSend).where(OutboundSend.campaign_id == campaign.id)
        )
    ).scalar_one()
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    # A single Core-level UPDATE with every value explicit, so `onupdate` (which only
    # fills in a column the statement did NOT already set) cannot silently overwrite the
    # "old" timestamp this test needs to exercise the staleness check.
    await session.execute(
        sa.update(OutboundSend)
        .where(OutboundSend.id == row.id)
        .values(status="sending", message_id=None, updated_at=old)
    )
    await session.commit()

    # Campaign is still "draft" here (never started) - this exercises the requeue helper
    # in isolation, matching the plan's unit-test bullet.
    requeued = await outbound_svc._requeue_stale_sending(session, datetime.now(timezone.utc))
    assert requeued == 1
    await session.refresh(row)
    assert row.status == "queued"


# ----------------------------------------------------------------------------------
# Integration
# ----------------------------------------------------------------------------------
async def test_campaign_send_does_not_flip_active_ai_thread(app_with_loopback, session):
    """DR-3/BULK_SEND_KEY: a bulk campaign send must never look like a human takeover."""
    client, carrier, _app = app_with_loopback
    _token, org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [{"e164": CONTACT}])
    campaign = await _campaign(session, org_id, lst.id)

    set_org_context(session, org_id)
    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164=OUR, contact_e164=CONTACT,
        ai_state="active", ai_armed_at=datetime.now(timezone.utc),
    )
    session.add(thread)
    await session.commit()

    campaign = await outbound_svc.start_campaign(session, campaign)
    counts = await outbound_svc.outbound_tick(session, carrier, None, Random(1), now=FROZEN)
    assert counts["sent"] == 1

    await session.refresh(thread)
    assert thread.ai_state == "active"


async def test_carrier_failure_retries_then_fails(app_with_carrier, session):
    client, fake, _app = app_with_carrier
    _token, org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [{"e164": CONTACT}])
    campaign = await _campaign(session, org_id, lst.id, max_attempts=2, retry_backoff_minutes=60)

    reject = SendResult(
        "rejected", None, CarrierError("carrier_transient", "500", True, "boom")
    )
    fake.scripted = [reject, reject]

    campaign = await outbound_svc.start_campaign(session, campaign)
    now = FROZEN

    first = await outbound_svc.outbound_tick(session, fake, None, Random(1), now=now)
    assert first["sent"] == 0
    assert first["failed"] == 0

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(OutboundSend).where(OutboundSend.campaign_id == campaign.id)
        )
    ).scalar_one()
    assert row.status == "queued"
    assert row.attempts == 1
    assert row.next_attempt_at is not None

    later = now + timedelta(hours=2)
    second = await outbound_svc.outbound_tick(session, fake, None, Random(1), now=later)
    assert second["failed"] == 1

    await session.refresh(row)
    assert row.status == "failed"
    assert row.attempts == 2


async def test_pause_stops_further_sends_mid_run(app_with_loopback, session):
    """Pause a campaign AFTER it has already sent something, then confirm ticking it
    again - still mid-run, with un-sent rows left - does nothing further."""
    client, carrier, _app = app_with_loopback
    _token, org_id = await _make_org(client)
    entries = [{"e164": f"+1214555{i:04d}"} for i in range(3)]
    lst = await _ready_list(session, org_id, entries)
    # A single sending number and a bounded rate: pacing lets only ONE row through per
    # tick call, which is exactly what makes "mid-run" (sent + still-queued rows both
    # present) reproducible without a background clock.
    campaign = await _campaign(session, org_id, lst.id, rate_per_minute=6)
    campaign = await outbound_svc.start_campaign(session, campaign)

    first = await outbound_svc.outbound_tick(session, carrier, None, Random(1), now=FROZEN)
    assert first["sent"] == 1

    await session.refresh(campaign)
    campaign = await outbound_svc.pause_campaign(session, campaign)
    assert campaign.status == "paused"

    second = await outbound_svc.outbound_tick(session, carrier, None, Random(1), now=FROZEN)
    assert second["sent"] == 0

    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(OutboundSend).where(OutboundSend.campaign_id == campaign.id)
        )
    ).scalars().all()
    assert sum(1 for r in rows if r.status == "sent") == 1
    assert sum(1 for r in rows if r.status == "queued") == 2  # never touched again

    with pytest.raises(ConflictError):
        await outbound_svc.pause_campaign(session, campaign)


async def test_per_row_message_fallback_and_skip(app_with_loopback, session):
    """DR-14: empty campaign body -> per-row `message` field; both empty -> skipped."""
    client, carrier, _app = app_with_loopback
    _token, org_id = await _make_org(client)
    with_message = "+12145550201"
    without_message = "+12145550202"
    lst = await _ready_list(
        session, org_id,
        [
            {"e164": with_message, "fields": {"message": "hi there"}},
            {"e164": without_message, "fields": {}},
        ],
    )
    campaign = await _campaign(session, org_id, lst.id, body=None)
    campaign = await outbound_svc.start_campaign(session, campaign)

    # Both rows share the campaign's single sending number, so per-number pacing lets only
    # ONE of them through per tick call - two calls, `now` advanced past the pacing
    # interval, are needed to see both outcomes land.
    now = FROZEN
    first = await outbound_svc.outbound_tick(session, carrier, None, Random(1), now=now)
    second = await outbound_svc.outbound_tick(
        session, carrier, None, Random(1), now=now + timedelta(minutes=1)
    )
    counts = {k: first.get(k, 0) + second.get(k, 0) for k in set(first) | set(second)}
    assert counts["sent"] == 1
    assert counts["skipped"] == 1

    set_org_context(session, org_id)
    rows = {
        r.e164: r
        for r in (
            await session.execute(
                sa.select(OutboundSend).where(OutboundSend.campaign_id == campaign.id)
            )
        ).scalars().all()
    }
    assert rows[with_message].status == "sent"
    assert rows[without_message].status == "skipped"
    assert rows[without_message].last_error == "no message"


async def test_campaign_body_renders_row_fields(app_with_loopback, session):
    """DR-14: the campaign body renders with the row's fields via the P3 renderer."""
    client, carrier, _app = app_with_loopback
    _token, org_id = await _make_org(client)
    lst = await _ready_list(
        session, org_id, [{"e164": CONTACT, "fields": {"first_name": "Priya"}}]
    )
    campaign = await _campaign(session, org_id, lst.id, body="Hi {{contact.first_name}}!")
    campaign = await outbound_svc.start_campaign(session, campaign)

    await outbound_svc.outbound_tick(session, carrier, None, Random(1), now=FROZEN)

    set_org_context(session, org_id)
    message = (
        await session.execute(sa.select(Message).where(Message.to_e164 == CONTACT))
    ).scalar_one()
    assert message.body == "Hi Priya!"


async def test_campaign_auto_completes(app_with_loopback, session):
    client, carrier, _app = app_with_loopback
    _token, org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [{"e164": CONTACT}])
    campaign = await _campaign(session, org_id, lst.id)
    campaign = await outbound_svc.start_campaign(session, campaign)

    counts = await outbound_svc.outbound_tick(session, carrier, None, Random(1), now=FROZEN)
    assert counts["completed_campaigns"] == 1

    await session.refresh(campaign)
    assert campaign.status == "completed"


async def test_daily_cap_limits_sends_per_number(app_with_loopback, session):
    """Integration proof the daily cap actually binds - not just that the pure pacing
    function is correct in isolation."""
    client, carrier, _app = app_with_loopback
    _token, org_id = await _make_org(client)
    entries = [{"e164": f"+1214555{i:04d}"} for i in range(2)]
    lst = await _ready_list(session, org_id, entries)
    campaign = await _campaign(session, org_id, lst.id, daily_cap=1, respect_warmup=False)
    campaign = await outbound_svc.start_campaign(session, campaign)

    await outbound_svc.outbound_tick(session, carrier, None, Random(1), now=FROZEN)
    await outbound_svc.outbound_tick(session, carrier, None, Random(1), now=FROZEN)

    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(OutboundSend).where(OutboundSend.campaign_id == campaign.id)
        )
    ).scalars().all()
    assert sum(1 for r in rows if r.status == "sent") == 1
    assert sum(1 for r in rows if r.status == "queued") == 1


async def test_warmup_ramp_caps_lower_than_daily_cap(app_with_loopback, session):
    """A 1-day-old number's ramp cap (50/day, per pacing.DEFAULT_WARMUP_SCHEDULE) must
    bind even though the campaign's own daily_cap (200) alone would not have."""
    client, carrier, _app = app_with_loopback
    _token, org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [{"e164": CONTACT}])
    campaign = await _campaign(session, org_id, lst.id, daily_cap=200, respect_warmup=True)

    set_org_context(session, org_id)
    now = FROZEN
    await session.execute(
        sa.update(OrgNumber)
        .where(OrgNumber.e164 == OUR)
        .values(warmup_started_at=now - timedelta(days=1))
    )
    # Simulate 50 messages already sent today from this number directly, rather than
    # driving 50 real tick calls through pacing just to reach the ramp ceiling.
    thread = MessageThread(
        id=uuid.uuid4(), org_id=org_id, our_e164=OUR, contact_e164="+19999999999"
    )
    session.add(thread)
    await session.flush()
    for i in range(50):
        session.add(
            Message(
                id=uuid.uuid4(), org_id=org_id, thread_id=thread.id, direction="outbound",
                status="accepted", from_e164=OUR, to_e164=f"+1999999{i:04d}", body="x",
            )
        )
    await session.commit()

    campaign = await outbound_svc.start_campaign(session, campaign)
    counts = await outbound_svc.outbound_tick(session, carrier, None, Random(1), now=FROZEN)
    # The ramp cap (50) is already reached, even though daily_cap alone (200) would not
    # have blocked this send - proving respect_warmup picks the LOWER of the two.
    assert counts["sent"] == 0


async def test_send_time_dnc_block_after_import(app_with_loopback, session):
    """DR-2: the compliance GATE is the authority at send time, not merely the import
    pre-filter - a contact DNC'd AFTER enqueue must still be blocked when the campaign
    actually tries to send to them."""
    client, carrier, _app = app_with_loopback
    token, org_id = await _make_org(client)
    h = auth_headers(token, str(org_id))
    lst = await _ready_list(session, org_id, [{"e164": CONTACT}])
    campaign = await _campaign(session, org_id, lst.id)
    campaign = await outbound_svc.start_campaign(session, campaign)

    r = await client.post("/api/v1/compliance/dnc", json={"e164": CONTACT}, headers=h)
    assert r.status_code == 201, r.text

    counts = await outbound_svc.outbound_tick(session, carrier, None, Random(1), now=FROZEN)
    assert counts["blocked"] == 1

    set_org_context(session, org_id)
    row = (
        await session.execute(
            sa.select(OutboundSend).where(OutboundSend.campaign_id == campaign.id)
        )
    ).scalar_one()
    assert row.status == "blocked"


# ----------------------------------------------------------------------------------
# THE GATE: a large mixed list, imported and run to completion end to end.
# ----------------------------------------------------------------------------------
async def _build_gate_csv() -> tuple[bytes, dict, list[str], list[str]]:
    """440 unique valid contacts (5 flagged to become quiet-hours deferrals after import),
    ~30 invalid rows, ~20 duplicates of earlier valid rows, and 10 rows pre-seeded DNC."""
    valid = [f"+1214555{i:04d}" for i in range(440)]
    quiet_hours_subset = valid[:5]
    dnc_subset = [f"+1469555{i:04d}" for i in range(10)]
    invalid = [f"not-a-number-{i}" for i in range(30)]
    duplicates = valid[:20]  # re-appear later in the file; first occurrence wins

    lines = ["phone,message"]
    for e164 in valid:
        lines.append(f"{e164},Hello there")
    for e164 in dnc_subset:
        lines.append(f"{e164},Hello there")
    for bogus in invalid:
        lines.append(f"{bogus},Hello there")
    for e164 in duplicates:
        lines.append(f"{e164},Hello there")

    csv_bytes = ("\n".join(lines) + "\n").encode()
    mapping = {"phone": "phone", "message": "message"}
    return csv_bytes, mapping, dnc_subset, quiet_hours_subset


async def test_the_gate_500_row_list_and_campaign_to_completion(app_with_loopback):
    client, carrier, app = app_with_loopback
    from app.db.session import get_sessionmaker

    token, org_id = await _make_org(client)
    h = auth_headers(token, str(org_id))

    # A pacing gate admits at most ONE send per sending number per tick call (the pacing
    # check runs against a single frozen `now` for the whole call). A spread of numbers
    # is what lets this test drain 440 rows in a sane number of tick calls instead of
    # needing one call per row.
    extra_numbers = [f"+1972555{9000 + i:04d}" for i in range(9)]
    for e164 in extra_numbers:
        r = await client.post("/api/v1/numbers", json={"e164": e164}, headers=h)
        assert r.status_code == 201, r.text

    csv_bytes, mapping, dnc_subset, quiet_hours_subset = await _build_gate_csv()
    for e164 in dnc_subset:
        r = await client.post("/api/v1/compliance/dnc", json={"e164": e164}, headers=h)
        assert r.status_code == 201, r.text

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        set_org_context(session, org_id)
        lst = ContactList(
            id=uuid.uuid4(), org_id=org_id, name="Gate list", source_filename="gate.csv",
            status="importing",
        )
        session.add(lst)
        await session.commit()
        list_id = lst.id

    await list_import_svc.run_import(
        sessionmaker, list_id=list_id, org_id=org_id, filename="gate.csv",
        data=csv_bytes, mapping=mapping,
    )

    async with sessionmaker() as session:
        set_org_context(session, org_id)
        refreshed = await session.get(ContactList, list_id)
        assert refreshed.status == "ready"
        assert refreshed.total_rows == 500
        assert refreshed.accepted_count == 440
        assert refreshed.invalid_count == 30
        assert refreshed.duplicate_count == 20
        assert refreshed.dnc_count == 10
        assert (
            refreshed.accepted_count
            + refreshed.invalid_count
            + refreshed.duplicate_count
            + refreshed.dnc_count
            == refreshed.total_rows
        )

        # Force a handful of accepted contacts into a timezone where the frozen clock
        # (18:00 UTC = 21:00 in Moscow, the window's own upper bound - not < 21:00) falls
        # OUTSIDE the default 08:00-21:00 window, so the campaign really does defer some
        # sends under quiet hours rather than merely being ABLE to.
        await session.execute(
            sa.update(Contact)
            .where(
                Contact.id.in_(
                    sa.select(ContactListRow.contact_id).where(
                        ContactListRow.list_id == list_id,
                        ContactListRow.e164.in_(quiet_hours_subset),
                    )
                )
            )
            .values(timezone="Europe/Moscow")
        )
        await session.commit()

    async with sessionmaker() as session:
        set_org_context(session, org_id)
        campaign = await outbound_svc.create_campaign(
            session, org_id, name="Gate campaign", channel="sms", list_id=list_id,
            # Empty from_numbers = the campaign's full active pool (all 10 numbers).
            body=None, from_numbers=[], rate_per_minute=6000, daily_cap=1000,
            respect_warmup=False, max_attempts=1, retry_backoff_minutes=1,
        )
        campaign = await outbound_svc.start_campaign(session, campaign)
        campaign_id = campaign.id

    # Drain in rounds, based on the FROZEN instant (not wall-clock - see module docstring
    # constant). At most one row per NUMBER can send per tick call (pacing runs against
    # one frozen `now` per call), so with 10 numbers this needs on the order of 440/10
    # rounds; a stalled round (nothing sent or deferred - e.g. every remaining number is
    # still inside this round's pacing window) jumps the clock a full day - preserving the
    # hour, so the quiet-hours math above stays exactly as deterministic as it started -
    # which the generous daily_cap here never actually needs to matter for.
    #
    # NOTE: `_last_send_at`'s pacing lookup reads REAL Message.created_at (TimestampMixin
    # is not frozen), so once FROZEN is further in the past than the real wall-clock this
    # test happens to run at, the first several rounds are pure day-jumps just to catch
    # `now` back up past "real now" before pacing can admit a second send per number - the
    # round budget below is sized generously to absorb that gap regardless of how far in
    # the future FROZEN eventually sits.
    now = FROZEN
    ROUNDS = 1000
    for _ in range(ROUNDS):
        async with sessionmaker() as session:
            counts = await outbound_svc.outbound_tick(session, carrier, None, Random(7), now=now)
        async with sessionmaker() as session:
            set_org_context(session, org_id)
            still_open = (
                await session.execute(
                    sa.select(sa.func.count()).select_from(OutboundSend).where(
                        OutboundSend.campaign_id == campaign_id,
                        OutboundSend.status.notin_(SEND_TERMINAL),
                    )
                )
            ).scalar_one()
        if still_open == 0:
            break
        if counts["sent"] == 0 and counts["deferred"] == 0:
            now += timedelta(days=1)
    else:
        pytest.fail(f"outbound_tick did not drain the campaign within {ROUNDS} rounds")

    async with sessionmaker() as session:
        set_org_context(session, org_id)
        rows = (
            await session.execute(
                sa.select(OutboundSend).where(OutboundSend.campaign_id == campaign_id)
            )
        ).scalars().all()
        assert len(rows) == 440
        assert all(r.status in SEND_TERMINAL for r in rows)
        sent = [r for r in rows if r.status == "sent"]
        deferred = [r for r in rows if r.status == "deferred"]
        assert len(sent) > 0
        assert len(deferred) >= len(quiet_hours_subset)

    # Per-row outcomes are queryable via the API.
    r = await client.get(
        f"/api/v1/outbound/lists/{list_id}/rows",
        params={"status": "dnc", "limit": 20}, headers=h,
    )
    assert r.status_code == 200
    assert len(r.json()) == 10

    r = await client.get(f"/api/v1/outbound/campaigns/{campaign_id}/progress", headers=h)
    assert r.status_code == 200
    progress = r.json()
    assert progress["total"] == 440
    assert progress["counts"].get("sent", 0) > 0
    assert progress["counts"].get("deferred", 0) >= len(quiet_hours_subset)
