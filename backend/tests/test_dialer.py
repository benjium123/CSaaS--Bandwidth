"""P11: the auto-dialer (services/dialer.py). DR-10..DR-13."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from random import Random

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.db.base import set_org_context
from app.models import (
    Call,
    Contact,
    ContactList,
    ContactListRow,
    ContactPhone,
    DialAttempt,
    OutboundCampaign,
)
from app.services import dialer as dialer_svc
from tests.conftest import make_org_with_number

OUR = "+12145550100"
A = "+19725550101"
B = "+19725550102"
C = "+19725550103"

#: A fixed instant, NOT wall-clock time - Moscow (UTC+3, no DST) is 21:00 here, the
#: window's own upper bound and not < 21:00, so quiet-hours tests that rely on that
#: contact being outside the allowed window stay true regardless of when CI runs.
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


async def _make_org(client) -> uuid.UUID:
    _token, org, _ = await make_org_with_number(client, "dl1@example.com", "Org A", OUR)
    return uuid.UUID(org["id"])


async def _ready_list(session, org_id: uuid.UUID, e164s: list[str]) -> ContactList:
    set_org_context(session, org_id)
    lst = ContactList(
        id=uuid.uuid4(), org_id=org_id, name="L", source_filename="l.csv", status="ready",
        total_rows=len(e164s), accepted_count=len(e164s),
    )
    session.add(lst)
    await session.flush()
    for e164 in e164s:
        contact = Contact(id=uuid.uuid4(), org_id=org_id, display_name=e164)
        session.add(contact)
        await session.flush()
        # Mirrors resolve_or_create_contact's own shape (services/contacts.py): quiet-hours
        # timezone lookup joins through ContactPhone, so a contact without one here would
        # silently fall back to NPA inference instead of the timezone this test sets below.
        session.add(
            ContactPhone(
                id=uuid.uuid4(), org_id=org_id, contact_id=contact.id, e164=e164,
                label="mobile", is_primary=True,
            )
        )
        await session.flush()
        session.add(
            ContactListRow(
                id=uuid.uuid4(), org_id=org_id, list_id=lst.id, row_number=1,
                raw={"phone": e164}, e164=e164, contact_id=contact.id, status="accepted",
                fields={},
            )
        )
    await session.commit()
    return lst


async def _dial_campaign(session, org_id, list_id, **overrides) -> OutboundCampaign:
    fields = {
        "name": "D", "channel": "voice", "list_id": list_id, "from_numbers": [OUR],
        "dialer_mode": "power", "parallel_lines": 1, "max_attempts": 2,
        "retry_backoff_minutes": 60, "local_presence": False,
    }
    fields.update(overrides)
    campaign = OutboundCampaign(id=uuid.uuid4(), org_id=org_id, **fields)
    session.add(campaign)
    await session.commit()
    return campaign


def _fake_start_call(outcomes: dict[str, dialer_svc.DialOutcome]):
    calls: list[str] = []

    async def _fake(session, settings, bus, api, *, org_id, to_e164, from_e164, identity):
        calls.append(to_e164)
        return outcomes[to_e164]

    _fake.calls = calls
    return _fake


# ----------------------------------------------------------------------------------
# Unit: enqueue idempotency / uniqueness
# ----------------------------------------------------------------------------------
async def test_enqueue_dial_rows_idempotent(app_with_loopback, session):
    client, _carrier, _app = app_with_loopback
    org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [A])
    campaign = await _dial_campaign(session, org_id, lst.id)

    first = await dialer_svc.enqueue_dial_rows(session, campaign)
    second = await dialer_svc.enqueue_dial_rows(session, campaign)
    assert first == 1
    assert second == 0


async def test_unique_constraint_blocks_duplicate_dial_row(app_with_loopback, session):
    client, _carrier, _app = app_with_loopback
    org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [A])
    campaign = await _dial_campaign(session, org_id, lst.id)
    await dialer_svc.enqueue_dial_rows(session, campaign)

    set_org_context(session, org_id)
    session.add(
        DialAttempt(
            id=uuid.uuid4(), org_id=org_id, campaign_id=campaign.id, e164=A, status="queued"
        )
    )
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


# ----------------------------------------------------------------------------------
# Modes / dispositions (DR-10, DR-12)
# ----------------------------------------------------------------------------------
@pytest.fixture
def _monkeypatch_start_call(monkeypatch):
    def _apply(outcomes):
        fake = _fake_start_call(outcomes)
        monkeypatch.setattr(dialer_svc, "_start_call", fake)
        return fake

    return _apply


async def test_power_mode_dials_one_at_a_time_in_claim_order(
    app_with_loopback, session, _monkeypatch_start_call
):
    client, carrier, _app = app_with_loopback
    org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [A, B, C])
    campaign = await _dial_campaign(session, org_id, lst.id, dialer_mode="power", parallel_lines=1)
    campaign = await dialer_svc.start_dial_campaign(session, campaign)

    fake = _monkeypatch_start_call(
        {
            A: dialer_svc.DialOutcome(status="connected", call_id=None),
            B: dialer_svc.DialOutcome(status="connected", call_id=None),
            C: dialer_svc.DialOutcome(status="connected", call_id=None),
        }
    )

    counts = await dialer_svc.dialer_tick(session, None, None, None, Random(1), now=FROZEN)
    assert counts["connected"] == 1
    assert fake.calls == [A]  # power mode = 1 line: only the FIRST queued row this round

    counts = await dialer_svc.dialer_tick(session, None, None, None, Random(1), now=FROZEN)
    assert counts["connected"] == 1
    assert fake.calls == [A, B]


async def test_parallel_mode_first_connected_wins_siblings_abandoned(
    app_with_loopback, session, _monkeypatch_start_call, monkeypatch
):
    client, carrier, _app = app_with_loopback
    org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [A, B, C])
    campaign = await _dial_campaign(
        session, org_id, lst.id, dialer_mode="parallel", parallel_lines=3, max_attempts=2
    )
    campaign = await dialer_svc.start_dial_campaign(session, campaign)

    set_org_context(session, org_id)
    # C is the LOSING sibling below - a real Call row so the hangup path has something to
    # load and end, proving a live human is not simply left connected on a dead row.
    losing_call = Call(
        id=uuid.uuid4(), org_id=org_id, direction="outbound", contact_e164=C, our_e164=OUR,
        carrier="telnyx", status="answered",
    )
    session.add(losing_call)
    await session.commit()

    _monkeypatch_start_call(
        {
            A: dialer_svc.DialOutcome(status="no_answer"),
            B: dialer_svc.DialOutcome(status="connected", call_id=None),
            C: dialer_svc.DialOutcome(status="connected", call_id=losing_call.id),
        }
    )

    hangups: list[tuple[object, uuid.UUID]] = []

    async def _fake_end_room_call(api, call):
        hangups.append((api, call.id))

    monkeypatch.setattr(dialer_svc.voice_plane_svc, "end_room_call", _fake_end_room_call)

    sentinel_api = object()
    counts = await dialer_svc.dialer_tick(session, sentinel_api, None, None, Random(1), now=FROZEN)
    assert counts["connected"] == 1
    assert counts["abandoned"] == 1

    # end_room_call fired for the LOSING call only.
    assert hangups == [(sentinel_api, losing_call.id)]

    set_org_context(session, org_id)
    rows = {
        r.e164: r
        for r in (
            await session.execute(
                sa.select(DialAttempt).where(DialAttempt.campaign_id == campaign.id)
            )
        ).scalars().all()
    }
    assert rows[B].status == "connected"  # first connected (claim order A,B,C) wins
    assert rows[C].status == "abandoned"  # sibling hung up
    assert rows[A].status == "queued"  # no_answer -> retryable
    assert rows[A].attempts == 1
    assert rows[A].next_attempt_at is not None


async def test_voicemail_amd_verdict_no_retry(app_with_loopback, session, _monkeypatch_start_call):
    client, carrier, _app = app_with_loopback
    org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [A])
    campaign = await _dial_campaign(session, org_id, lst.id, max_attempts=3)
    campaign = await dialer_svc.start_dial_campaign(session, campaign)

    _monkeypatch_start_call(
        {A: dialer_svc.DialOutcome(status="connected", call_id=None, amd_verdict="machine")}
    )

    counts = await dialer_svc.dialer_tick(session, None, None, None, Random(1), now=FROZEN)
    assert counts["voicemail"] == 1

    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(DialAttempt).where(DialAttempt.campaign_id == campaign.id))
    ).scalar_one()
    assert row.status == "voicemail"
    assert row.disposition == "voicemail"
    assert row.next_attempt_at is None  # never retried


async def test_no_answer_schedules_a_retry(app_with_loopback, session, _monkeypatch_start_call):
    client, carrier, _app = app_with_loopback
    org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [A])
    campaign = await _dial_campaign(
        session, org_id, lst.id, max_attempts=2, retry_backoff_minutes=90
    )
    campaign = await dialer_svc.start_dial_campaign(session, campaign)

    _monkeypatch_start_call({A: dialer_svc.DialOutcome(status="no_answer")})

    now = FROZEN
    await dialer_svc.dialer_tick(session, None, None, None, Random(1), now=now)

    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(DialAttempt).where(DialAttempt.campaign_id == campaign.id))
    ).scalar_one()
    assert row.status == "queued"
    assert row.attempts == 1
    expected = now + timedelta(minutes=90)
    actual = row.next_attempt_at
    if actual.tzinfo is None:  # SQLite round-trips DateTime(timezone=True) as naive
        actual = actual.replace(tzinfo=timezone.utc)
    assert actual == expected


async def test_quiet_hours_defers_without_dialing(
    app_with_loopback, session, _monkeypatch_start_call
):
    """DR-11: quiet hours defers `next_attempt_at` and never dials - the SAME compliance
    primitives the SMS gate uses, not a re-implementation."""
    client, carrier, _app = app_with_loopback
    org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [A])
    campaign = await _dial_campaign(session, org_id, lst.id)
    campaign = await dialer_svc.start_dial_campaign(session, campaign)

    set_org_context(session, org_id)
    row_id = (
        await session.execute(
            sa.select(ContactListRow.contact_id).where(
                ContactListRow.list_id == lst.id, ContactListRow.e164 == A
            )
        )
    ).scalar_one()
    await session.execute(
        sa.update(Contact).where(Contact.id == row_id).values(timezone="Europe/Moscow")
    )
    await session.commit()

    fake = _monkeypatch_start_call({A: dialer_svc.DialOutcome(status="connected")})

    counts = await dialer_svc.dialer_tick(session, None, None, None, Random(1), now=FROZEN)
    assert counts["deferred"] == 1
    assert fake.calls == []  # never dialed

    row = (
        await session.execute(sa.select(DialAttempt).where(DialAttempt.campaign_id == campaign.id))
    ).scalar_one()
    assert row.status == "queued"
    assert row.next_attempt_at is not None


async def test_dnc_contact_never_dialed(app_with_loopback, session, _monkeypatch_start_call):
    client, carrier, app = app_with_loopback
    org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [A])
    campaign = await _dial_campaign(session, org_id, lst.id)
    campaign = await dialer_svc.start_dial_campaign(session, campaign)

    set_org_context(session, org_id)
    from app.compliance import service as compliance_svc

    await compliance_svc.add_dnc(session, org_id, A)
    await session.commit()

    fake = _monkeypatch_start_call({A: dialer_svc.DialOutcome(status="connected")})

    await dialer_svc.dialer_tick(session, None, None, None, Random(1), now=FROZEN)
    assert fake.calls == []

    row = (
        await session.execute(sa.select(DialAttempt).where(DialAttempt.campaign_id == campaign.id))
    ).scalar_one()
    assert row.status == "failed"  # permanent compliance block, never retried
    assert row.next_attempt_at is None


async def test_stale_dialing_rows_are_requeued(app_with_loopback, session):
    client, carrier, _app = app_with_loopback
    org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [A])
    campaign = await _dial_campaign(session, org_id, lst.id)
    await dialer_svc.enqueue_dial_rows(session, campaign)

    set_org_context(session, org_id)
    row = (
        await session.execute(sa.select(DialAttempt).where(DialAttempt.campaign_id == campaign.id))
    ).scalar_one()
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    await session.execute(
        sa.update(DialAttempt)
        .where(DialAttempt.id == row.id)
        .values(status="dialing", call_id=None, updated_at=old)
    )
    await session.commit()

    requeued = await dialer_svc._requeue_stale_dialing(session, datetime.now(timezone.utc))
    assert requeued == 1
    await session.refresh(row)
    assert row.status == "queued"


# ----------------------------------------------------------------------------------
# Predictive pacing (DR-10): coefficient drops after abandons, throttling the round.
# ----------------------------------------------------------------------------------
async def test_predictive_pacing_slows_after_abandons(
    app_with_loopback, session, _monkeypatch_start_call
):
    client, carrier, _app = app_with_loopback
    org_id = await _make_org(client)
    entries = [f"+1214555{i:04d}" for i in range(10)]
    lst = await _ready_list(session, org_id, entries)
    campaign = await _dial_campaign(
        session, org_id, lst.id, dialer_mode="predictive", parallel_lines=10, max_attempts=1
    )

    set_org_context(session, org_id)
    # Seed a heavy abandon history for THIS campaign (>= pacing.predictive_coefficient's
    # min_sample of 20 placed calls, most of them abandoned) so the coefficient is pulled
    # down to its floor before any of today's rows are even claimed.
    for i in range(20):
        session.add(
            DialAttempt(
                id=uuid.uuid4(), org_id=org_id, campaign_id=campaign.id,
                e164=f"+1999000{i:04d}", status="completed",
            )
        )
    for i in range(15):
        session.add(
            DialAttempt(
                id=uuid.uuid4(), org_id=org_id, campaign_id=campaign.id,
                e164=f"+1999111{i:04d}", status="abandoned",
            )
        )
    await session.commit()

    campaign = await dialer_svc.start_dial_campaign(session, campaign)

    fake = _monkeypatch_start_call(
        {e164: dialer_svc.DialOutcome(status="connected", call_id=None) for e164 in entries}
    )

    await dialer_svc.dialer_tick(session, None, None, None, Random(1), now=FROZEN)

    # power/parallel with parallel_lines=10 would attempt up to 10 today's rows in one
    # round; the heavy abandon history must pull that well below 10.
    assert 0 < len(fake.calls) < 10


# ----------------------------------------------------------------------------------
# Preview mode's manual trigger (DR-10): without this, a preview campaign never dials.
# ----------------------------------------------------------------------------------
async def test_dial_next_connected(app_with_loopback, session, _monkeypatch_start_call):
    client, carrier, _app = app_with_loopback
    org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [A])
    campaign = await _dial_campaign(session, org_id, lst.id, dialer_mode="preview")
    campaign = await dialer_svc.start_dial_campaign(session, campaign)

    fake = _monkeypatch_start_call({A: dialer_svc.DialOutcome(status="connected", call_id=None)})

    row = await dialer_svc.dial_next(session, object(), None, None, campaign, now=FROZEN)
    assert row is not None
    assert row.status == "connected"
    assert row.attempts == 1
    assert fake.calls == [A]


async def test_dial_next_voicemail(app_with_loopback, session, _monkeypatch_start_call):
    client, carrier, _app = app_with_loopback
    org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [A])
    campaign = await _dial_campaign(session, org_id, lst.id, dialer_mode="preview")
    campaign = await dialer_svc.start_dial_campaign(session, campaign)

    _monkeypatch_start_call(
        {A: dialer_svc.DialOutcome(status="connected", call_id=None, amd_verdict="machine")}
    )

    row = await dialer_svc.dial_next(session, object(), None, None, campaign, now=FROZEN)
    assert row is not None
    assert row.status == "voicemail"
    assert row.disposition == "voicemail"


async def test_dial_next_returns_none_when_nothing_due(app_with_loopback, session):
    client, carrier, _app = app_with_loopback
    org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [])
    campaign = await _dial_campaign(session, org_id, lst.id, dialer_mode="preview")
    campaign = await dialer_svc.start_dial_campaign(session, campaign)

    row = await dialer_svc.dial_next(session, object(), None, None, campaign, now=FROZEN)
    assert row is None


async def test_dial_next_blocks_dnc_contact(app_with_loopback, session, _monkeypatch_start_call):
    client, carrier, _app = app_with_loopback
    org_id = await _make_org(client)
    lst = await _ready_list(session, org_id, [A])
    campaign = await _dial_campaign(session, org_id, lst.id, dialer_mode="preview")
    campaign = await dialer_svc.start_dial_campaign(session, campaign)

    set_org_context(session, org_id)
    from app.compliance import service as compliance_svc

    await compliance_svc.add_dnc(session, org_id, A)
    await session.commit()

    fake = _monkeypatch_start_call({A: dialer_svc.DialOutcome(status="connected")})

    row = await dialer_svc.dial_next(session, object(), None, None, campaign, now=FROZEN)
    assert row is not None
    assert row.status == "failed"
    assert row.disposition == "blocked"
    assert fake.calls == []
