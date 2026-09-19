"""Prepaid telephony hard gate (migrations 0041 and 0055, services/telephony_billing.py).

Prices here are the FLAT, carrier-independent platform prices the operator set
(telephony_billing.PLATFORM_PRICE_MICROS) - they no longer derive from any carrier's rate
card, so the same numbers apply whichever carrier carries the traffic.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Call, CreditLedgerEntry, Message, Org, OrgNumber, User
from app.models.spend import ProviderRate
from app.services import credits, telephony_billing
from app.services import messaging as messaging_svc
from app.services.telephony_billing import TelephonyCreditsError
from tests.conftest import TEST_PLATFORM_OPS_TOKEN, auth_headers, make_org_with_number

SMS_OUT = 10_000  # $0.01 per segment, flat
SMS_IN = 10_000
MMS_OUT = 10_000  # an MMS is one segment at the SMS price
VOICE_MIN = 5_000  # $0.005 per minute, billed BY THE SECOND
MIN_OUT = VOICE_MIN
MIN_IN = VOICE_MIN
NUMBER_MRC = 10_000_000  # $10.00 per number per month
NUMBER_SETUP = 0

OPS = {"X-Platform-Ops-Token": TEST_PLATFORM_OPS_TOKEN}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _new_org(session, name: str = "Prepaid Org") -> Org:
    org = Org(id=uuid.uuid4(), name=name, slug=f"pp-{uuid.uuid4().hex[:16]}")
    session.add(org)
    await session.commit()
    return org


async def _enable(session, org_id, *, balance: int = 0, since: datetime | None = None) -> Org:
    org = await session.get(Org, uuid.UUID(str(org_id)))
    org.telephony_prepaid = True
    org.telephony_prepaid_since = since or (_now() - timedelta(hours=1))
    await session.commit()
    if balance:
        await credits.topup(session, org.id, balance, reference=f"topup-{uuid.uuid4()}")
        await session.commit()
    return org


async def _usage(session, org_id) -> list[CreditLedgerEntry]:
    set_org_context(session, uuid.UUID(str(org_id)))
    return list(
        (
            await session.execute(
                sa.select(CreditLedgerEntry)
                .where(CreditLedgerEntry.entry_type == "usage")
                .order_by(CreditLedgerEntry.seq)
            )
        ).scalars()
    )


async def _balance(session, org_id) -> int:
    return await credits.balance(session, uuid.UUID(str(org_id)))


def _outbound_call(org_id, **extra) -> Call:
    return Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction=extra.pop("direction", "outbound"),
        contact_e164="+19725550100",
        our_e164="+12145550100",
        carrier="bandwidth",
        status=extra.pop("status", "queued"),
        extra={},
        **extra,
    )


# ==================================================================================
# SMS
# ==================================================================================
async def test_outbound_sms_refused_when_prepaid_balance_is_empty(app_with_carrier, session):
    client, fake, _app = app_with_carrier
    token, org, _n = await make_org_with_number(
        client, "pp-empty@example.com", "PP Empty", "+12145550901"
    )
    await _enable(session, org["id"])

    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725550901", "body": "hi"},
        headers=auth_headers(token, org["id"]),
    )

    assert r.status_code == 402, r.text
    assert r.json()["error"]["code"] == "insufficient_credits"
    assert fake.sent == [], "a refused send must never reach the carrier"
    set_org_context(session, uuid.UUID(org["id"]))
    assert (await session.execute(sa.select(sa.func.count(Message.id)))).scalar_one() == 0
    assert await _usage(session, org["id"]) == []


async def test_outbound_sms_is_charged_once_on_acceptance(app_with_carrier, session):
    client, fake, _app = app_with_carrier
    token, org, _n = await make_org_with_number(
        client, "pp-charge@example.com", "PP Charge", "+12145550902"
    )
    await _enable(session, org["id"], balance=1_000_000)

    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725550902", "body": "hi"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "accepted"
    message_id = r.json()["id"]

    rows = await _usage(session, org["id"])
    assert [(row.reference, -row.amount_micros) for row in rows] == [(f"sms:{message_id}", SMS_OUT)]
    assert await _balance(session, org["id"]) == 1_000_000 - SMS_OUT

    # A replayed charge (retry, double hook) is a no-op on the same reference.
    set_org_context(session, uuid.UUID(org["id"]))
    message = await session.get(Message, uuid.UUID(message_id))
    await telephony_billing.charge_sms(session, message.org_id, message)
    await session.commit()
    assert len(await _usage(session, org["id"])) == 1
    assert await _balance(session, org["id"]) == 1_000_000 - SMS_OUT


async def test_carrier_rejected_sms_is_not_charged(app_with_carrier, session):
    client, fake, _app = app_with_carrier
    token, org, _n = await make_org_with_number(
        client, "pp-rej@example.com", "PP Rejected", "+12145550903"
    )
    await _enable(session, org["id"], balance=1_000_000)
    fake.scripted.append(type(fake.default_result)("rejected", None, None))

    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725550903", "body": "hi"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "rejected"
    assert await _usage(session, org["id"]) == []
    assert await _balance(session, org["id"]) == 1_000_000


async def test_org_without_the_gate_is_never_charged_or_refused(app_with_carrier, session):
    client, fake, _app = app_with_carrier
    token, org, _n = await make_org_with_number(
        client, "pp-off@example.com", "PP Off", "+12145550904"
    )
    # No _enable: telephony_prepaid stays false, balance stays 0.

    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725550904", "body": "hi"},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    assert len(fake.sent) == 1
    set_org_context(session, uuid.UUID(org["id"]))
    assert (await session.execute(sa.select(sa.func.count(CreditLedgerEntry.id)))).scalar_one() == 0


async def test_scheduled_sms_is_refused_at_release_when_credit_ran_out(app_with_carrier, session):
    """Held/scheduled/campaign sends reach the carrier without send_message's early check,
    so the gate is re-checked at the moment of sending."""
    client, fake, app = app_with_carrier
    token, org, _n = await make_org_with_number(
        client, "pp-later@example.com", "PP Later", "+12145550905"
    )
    await _enable(session, org["id"], balance=SMS_OUT)
    when = _now() + timedelta(hours=1)

    r = await client.post(
        "/api/v1/messages",
        json={"to": "+19725550905", "body": "later", "scheduled_for": when.isoformat()},
        headers=auth_headers(token, org["id"]),
    )
    assert r.status_code == 201, r.text
    message_id = r.json()["id"]

    # The balance runs out before the message is due.
    await credits.adjust(
        session,
        uuid.UUID(org["id"]),
        -SMS_OUT,
        reference="drain",
        note="test drain",
        created_by=None,
    )
    await session.commit()

    await messaging_svc.release_scheduled_messages(
        session, fake, now=when + timedelta(minutes=1), registry=app.state.carriers
    )

    assert fake.sent == [], "no carrier call once the balance cannot cover it"
    set_org_context(session, uuid.UUID(org["id"]))
    row = await session.get(Message, uuid.UUID(message_id))
    assert row.status == "rejected"
    assert row.error_code == "insufficient_credits"
    assert row.failure_reason_public
    assert await _usage(session, org["id"]) == []


async def test_inbound_sms_is_charged_even_on_an_empty_balance(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    inbound = Message(
        id=uuid.uuid4(),
        org_id=org.id,
        thread_id=uuid.uuid4(),
        direction="inbound",
        status="received",
        from_e164="+19725550106",
        to_e164="+12145550106",
        body="hello",
        media=[],
        carrier="bandwidth",
        segment_count_carrier=2,
    )

    await telephony_billing.charge_sms(session, org.id, inbound)
    await session.commit()

    assert await _balance(session, org.id) == -2 * SMS_IN, "inbound is never refused"


async def test_extra_carrier_segments_are_charged_once(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    message = Message(
        id=uuid.uuid4(),
        org_id=org.id,
        thread_id=uuid.uuid4(),
        direction="outbound",
        status="accepted",
        from_e164="+12145550107",
        to_e164="+19725550107",
        body="x",
        media=[],
        carrier="bandwidth",
        segment_count_est=1,
    )
    await telephony_billing.charge_sms(session, org.id, message)
    await session.commit()

    message.segment_count_carrier = 3  # the delivery receipt's count
    await telephony_billing.charge_segment_correction(session, org.id, message)
    await telephony_billing.charge_segment_correction(session, org.id, message)
    await session.commit()

    assert await _balance(session, org.id) == 1_000_000 - 3 * SMS_OUT


# ==================================================================================
# Calls
# ==================================================================================
async def test_outbound_dial_refused_on_empty_balance_and_holds_minutes_when_funded(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    with pytest.raises(TelephonyCreditsError):
        await telephony_billing.require_call_credit(session, org.id, _outbound_call(org.id))

    await credits.topup(session, org.id, 1_000_000, reference="fund")
    await session.commit()
    await telephony_billing.require_call_credit(session, org.id, _outbound_call(org.id))
    await session.commit()
    assert (
        await _balance(session, org.id)
        == 1_000_000 - telephony_billing.CALL_RESERVE_MINUTES * MIN_OUT
    )


async def test_finished_call_is_billed_once_releases_its_hold_and_never_retro_bills(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    now = _now()

    call = _outbound_call(
        org.id,
        status="completed",
        answered_at=now - timedelta(minutes=3),
        ended_at=now - timedelta(minutes=1),
        duration_seconds=121,  # 121s x $0.005/min, rounded up = 10_084 micros
    )
    await telephony_billing.require_call_credit(session, org.id, call)
    session.add(call)
    # A call that started BEFORE the gate was switched on is never billed.
    old = _outbound_call(
        org.id,
        status="completed",
        answered_at=now - timedelta(hours=3),
        ended_at=now - timedelta(hours=2),
        duration_seconds=600,
        created_at=now - timedelta(hours=3),
    )
    session.add(old)
    await session.commit()

    assert await telephony_billing.bill_finished_calls(session) == 1
    assert await _balance(session, org.id) == 1_000_000 - 10_084, (
        "hold released, seconds charged"
    )
    assert [(-row.amount_micros) for row in await _usage(session, org.id)] == [10_084]
    set_org_context(session, org.id)
    assert (await session.get(Call, call.id)).billed_at is not None
    assert (await session.get(Call, old.id)).billed_at is None

    assert await telephony_billing.bill_finished_calls(session) == 0
    assert await _balance(session, org.id) == 1_000_000 - 10_084


async def test_inbound_call_seconds_are_charged_without_a_gate(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    now = _now()
    set_org_context(session, org.id)
    session.add(
        _outbound_call(
            org.id,
            direction="inbound",
            status="completed",
            answered_at=now - timedelta(minutes=2),
            ended_at=now - timedelta(minutes=1),
            duration_seconds=60,
        )
    )
    await session.commit()

    assert await telephony_billing.bill_finished_calls(session) == 1
    assert await _balance(session, org.id) == -5_000  # 60s at $0.005/min


async def test_running_outbound_call_is_hung_up_when_it_can_no_longer_be_funded(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=telephony_billing.CALL_RESERVE_MINUTES * MIN_OUT)
    call = _outbound_call(org.id, status="answered", answered_at=_now() - timedelta(minutes=10))
    await telephony_billing.require_call_credit(session, org.id, call)  # takes the whole balance
    session.add(call)
    await session.commit()

    hung: list[uuid.UUID] = []

    async def hangup(_session, c) -> None:
        hung.append(c.id)

    assert await telephony_billing.enforce_active_calls(session, hangup=hangup) == 1
    assert hung == [call.id]


async def test_running_outbound_call_gets_its_hold_extended_while_funded(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    call = _outbound_call(org.id, status="answered", answered_at=_now() - timedelta(minutes=5))
    await telephony_billing.require_call_credit(session, org.id, call)
    session.add(call)
    await session.commit()

    hung: list[uuid.UUID] = []

    async def hangup(_session, c) -> None:
        hung.append(c.id)

    assert await telephony_billing.enforce_active_calls(session, hangup=hangup) == 0
    assert hung == []
    held, holds = await telephony_billing._held_for_call(session, org.id, call.id)
    assert holds == 2
    assert held == 2 * telephony_billing.CALL_RESERVE_MINUTES * MIN_OUT


async def test_billing_ignores_calls_of_orgs_without_the_gate(session):
    org = await _new_org(session)  # gate off
    now = _now()
    call = _outbound_call(
        org.id,
        status="completed",
        answered_at=now - timedelta(minutes=2),
        ended_at=now,
        duration_seconds=90,
    )
    set_org_context(session, org.id)
    session.add(call)
    await session.commit()

    assert await telephony_billing.bill_finished_calls(session) == 0
    set_org_context(session, org.id)
    assert (await session.get(Call, call.id)).billed_at is None


# ==================================================================================
# Number rental
# ==================================================================================
async def test_number_order_refused_when_the_first_month_is_not_covered(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=NUMBER_MRC - 1)
    with pytest.raises(TelephonyCreditsError):
        await telephony_billing.require_number_credit(session, org.id, "bandwidth")


async def test_rental_charged_at_order_then_renewed_monthly_once_per_period(session):
    org = await _new_org(session)
    # This test simulates rental periods in early 2026, so the gate has to have been on
    # since before them - otherwise the (correct) out-of-scope guard in
    # renew_number_rentals skips them as predating our billing of this org.
    await _enable(
        session, org.id, balance=10_000_000, since=datetime(2025, 1, 1, tzinfo=timezone.utc)
    )
    set_org_context(session, org.id)
    number = OrgNumber(
        id=uuid.uuid4(), org_id=org.id, e164="+12145550199", carrier="bandwidth", status="active"
    )
    session.add(number)
    await session.flush()

    await telephony_billing.charge_new_number(session, org.id, number, today=date(2026, 1, 31))
    await session.commit()
    assert number.rental_paid_through == date(2026, 2, 28), "month-end clamps, never overflows"
    assert await _balance(session, org.id) == 10_000_000 - NUMBER_MRC

    assert await telephony_billing.renew_number_rentals(session, today=date(2026, 2, 28)) == 1
    set_org_context(session, org.id)
    assert (await session.get(OrgNumber, number.id)).rental_paid_through == date(2026, 3, 28)
    assert await telephony_billing.renew_number_rentals(session, today=date(2026, 2, 28)) == 0
    assert await _balance(session, org.id) == 10_000_000 - 2 * NUMBER_MRC


async def test_a_future_rental_stamp_delays_billing_instead_of_disabling_it(session):
    """Migration 0055 stamps rental_paid_through one month ahead for every number an org
    already held, so switching the prepaid gate on never bills a customer for a period we
    were not charging for. This pins the resulting property of renew_number_rentals: a
    future stamp is skipped, and the SAME number is charged once the date arrives.

    Both halves matter. A test that only proved "no charge" would pass just as well if
    number rental were broken outright.
    """
    org = await _new_org(session)
    await _enable(session, org.id)  # gate on, ZERO balance - as on migration day
    set_org_context(session, org.id)
    stamped = date(2026, 10, 19)
    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org.id,
        e164="+12145550198",
        carrier="bandwidth",
        status="active",
        rental_paid_through=stamped,
    )
    session.add(number)
    await session.commit()

    # Before the stamp: nothing is charged and the ledger stays empty.
    day_before = stamped - timedelta(days=1)
    assert await telephony_billing.renew_number_rentals(session, today=day_before) == 0
    assert await _usage(session, org.id) == []
    assert await _balance(session, org.id) == 0

    # On the stamped date: the natural cycle begins and it IS charged, exactly $10.
    assert await telephony_billing.renew_number_rentals(session, today=stamped) == 1
    assert [-row.amount_micros for row in await _usage(session, org.id)] == [NUMBER_MRC]
    assert await _balance(session, org.id) == -NUMBER_MRC
    set_org_context(session, org.id)
    assert (await session.get(OrgNumber, number.id)).rental_paid_through == date(2026, 11, 19)


async def test_a_released_number_is_never_renewed(session):
    """renew_number_rentals filters on status == 'active' and released_at IS NULL, which is
    why migration 0055 deliberately leaves those rows' rental_paid_through NULL - it would
    only be stamping rows nothing would ever charge."""
    org = await _new_org(session)
    await _enable(session, org.id)
    set_org_context(session, org.id)
    session.add(
        OrgNumber(
            id=uuid.uuid4(),
            org_id=org.id,
            e164="+12145550197",
            carrier="bandwidth",
            status="released",
            rental_paid_through=None,
        )
    )
    await session.commit()

    assert await telephony_billing.renew_number_rentals(session, today=date(2026, 10, 19)) == 0
    assert await _usage(session, org.id) == []


async def test_a_number_held_before_the_gate_went_on_is_not_billed_for_that_stretch(session):
    """The durable invariant, and the counterpart of _billable_org_filter for calls: a
    rental period that predates telephony_prepaid_since is never charged, however the row
    got there. The number is stamped forward instead, so billing is DELAYED, not waived.

    This is the property migration 0055's backfill also enforces for existing rows; the
    guard holds it for rows created afterwards by any route.
    """
    org = await _new_org(session)
    set_org_context(session, org.id)
    # Held for months while the org was NOT prepaid: never stamped, created long ago.
    number = OrgNumber(
        id=uuid.uuid4(),
        org_id=org.id,
        e164="+12145550196",
        carrier="bandwidth",
        status="active",
        rental_paid_through=None,
        created_at=_now() - timedelta(days=200),
    )
    session.add(number)
    await session.commit()
    # Only NOW does the gate go on, with a zero balance.
    await _enable(session, org.id)

    today = _now().date()
    # Half 1: the switch-on charges nothing at all.
    assert await telephony_billing.renew_number_rentals(session, today=today) == 0
    assert await _usage(session, org.id) == []
    assert await _balance(session, org.id) == 0
    set_org_context(session, org.id)
    stamped = (await session.get(OrgNumber, number.id)).rental_paid_through
    assert stamped == telephony_billing._next_month(today), "stamped forward one cycle"

    # Half 2: once that cycle arrives it IS charged - delayed, not disabled.
    assert await telephony_billing.renew_number_rentals(session, today=stamped) == 1
    assert [-row.amount_micros for row in await _usage(session, org.id)] == [NUMBER_MRC]


async def test_a_number_added_after_the_gate_went_on_is_still_billed(session):
    """The other side of the same guard: a number whose row was created while the gate was
    already on is in scope and charged from today, exactly as before. Without this, the
    out-of-scope rule would quietly make every imported number free forever."""
    org = await _new_org(session)
    await _enable(session, org.id)  # gate on FIRST
    set_org_context(session, org.id)
    session.add(
        OrgNumber(
            id=uuid.uuid4(),
            org_id=org.id,
            e164="+12145550195",
            carrier="bandwidth",
            status="active",
            rental_paid_through=None,
            created_at=_now(),  # after the stamp
        )
    )
    await session.commit()

    assert await telephony_billing.renew_number_rentals(session, today=_now().date()) == 1
    assert [-row.amount_micros for row in await _usage(session, org.id)] == [NUMBER_MRC]


async def test_an_org_with_no_prepaid_since_is_never_billed_for_rental(session):
    """Mirror of _billable_org_filter's `telephony_prepaid_since IS NOT NULL`: with no
    stamp we cannot prove any period is in scope, so we refuse to bill rather than guess."""
    org = await _new_org(session)
    set_org_context(session, org.id)
    session.add(
        OrgNumber(
            id=uuid.uuid4(),
            org_id=org.id,
            e164="+12145550194",
            carrier="bandwidth",
            status="active",
            rental_paid_through=None,
        )
    )
    org.telephony_prepaid = True
    org.telephony_prepaid_since = None  # gate on, but never stamped
    await session.commit()

    assert await telephony_billing.renew_number_rentals(session, today=_now().date()) == 0
    assert await _usage(session, org.id) == []


async def test_stamp_rentals_forward_stamps_only_unstamped_active_numbers(session):
    """What the platform ops switch-on calls so flipping one org's gate cannot bill them
    for numbers they already hold."""
    org = await _new_org(session)
    set_org_context(session, org.id)
    today = date(2026, 6, 15)
    unstamped = OrgNumber(
        id=uuid.uuid4(), org_id=org.id, e164="+12145550193", carrier="bandwidth",
        status="active", rental_paid_through=None,
    )
    already = OrgNumber(
        id=uuid.uuid4(), org_id=org.id, e164="+12145550192", carrier="bandwidth",
        status="active", rental_paid_through=date(2026, 8, 1),
    )
    released = OrgNumber(
        id=uuid.uuid4(), org_id=org.id, e164="+12145550191", carrier="bandwidth",
        status="released", rental_paid_through=None,
    )
    session.add_all([unstamped, already, released])
    await session.commit()

    assert await telephony_billing.stamp_rentals_forward(session, org.id, today=today) == 1
    await session.commit()
    set_org_context(session, org.id)
    assert (await session.get(OrgNumber, unstamped.id)).rental_paid_through == date(2026, 7, 15)
    assert (await session.get(OrgNumber, already.id)).rental_paid_through == date(2026, 8, 1)
    assert (await session.get(OrgNumber, released.id)).rental_paid_through is None
    # It never charges.
    assert await _usage(session, org.id) == []


# ==================================================================================
# Surfaces
# ==================================================================================
async def test_ops_toggle_turns_the_gate_on_and_restamps_since(app_with_carrier, session):
    client, _fake, _app = app_with_carrier
    token, org, _n = await make_org_with_number(
        client, "pp-ops@example.com", "PP Ops", "+12145550906"
    )
    url = f"/api/v1/platform/billing/orgs/{org['id']}"

    r = await client.patch(url, json={"telephony_prepaid": True}, headers=OPS)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["telephony_prepaid"] is True
    assert first["telephony_prepaid_since"] is not None

    r = await client.patch(url, json={"telephony_prepaid": False}, headers=OPS)
    assert r.json()["telephony_prepaid"] is False
    r = await client.patch(url, json={"telephony_prepaid": True}, headers=OPS)
    assert r.json()["telephony_prepaid_since"] >= first["telephony_prepaid_since"]

    summary = await client.get("/api/v1/billing/summary", headers=auth_headers(token, org["id"]))
    assert summary.status_code == 200, summary.text
    assert summary.json()["telephony_prepaid"] is True


# ==================================================================================
# Flat platform pricing
# ==================================================================================
async def test_flat_prices_are_exact_and_carrier_independent(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    # "unpriced-co" is a carrier with NO rate card at all (it is not in
    # spend.DEFAULT_RATES_MICROS) and is <= 16 characters, the width of
    # provider_rates.provider. Before flat pricing unit_price returned 0 for it, so its
    # traffic was silently free AND unblockable - this loop is the regression guard for it.
    for carrier in ("bandwidth", "twilio", "unpriced-co"):
        assert (
            await telephony_billing.unit_price(session, org.id, carrier, "sms_out") == 10_000
        )
        assert await telephony_billing.unit_price(session, org.id, carrier, "sms_in") == 10_000
        assert await telephony_billing.unit_price(session, org.id, carrier, "mms_out") == 10_000
        assert (
            await telephony_billing.unit_price(session, org.id, carrier, "voice_min_out")
            == 5_000
        )
        assert (
            await telephony_billing.unit_price(session, org.id, carrier, "voice_min_in")
            == 5_000
        )
        assert (
            await telephony_billing.unit_price(session, org.id, carrier, "number_mrc")
            == 10_000_000
        )
        assert await telephony_billing.unit_price(session, org.id, carrier, "number_setup") == 0


async def test_sms_price_is_exactly_ten_thousand_micros_per_segment(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    for segments, expected in ((1, 10_000), (2, 20_000), (3, 30_000), (10, 100_000)):
        assert (
            await telephony_billing.sms_price(
                session, org.id, carrier="bandwidth", segments=segments, is_mms=False
            )
            == expected
        )
    # An MMS is one segment at the SMS price whatever its segment count says.
    assert (
        await telephony_billing.sms_price(
            session, org.id, carrier="bandwidth", segments=5, is_mms=True
        )
        == 10_000
    )


def test_voice_price_micros_is_integer_ceiling_per_second():
    # charge = ceil(seconds x 5_000 / 60), integer arithmetic only, always rounded UP so
    # we never under-charge and never emit a fractional micro.
    cases = {0: 0, 1: 84, 2: 167, 30: 2_500, 59: 4_917, 60: 5_000, 61: 5_084, 3_600: 300_000}
    for seconds, expected in cases.items():
        assert telephony_billing.voice_price_micros(seconds, 5_000) == expected
    assert telephony_billing.voice_price_micros(None, 5_000) == 0
    assert telephony_billing.voice_price_micros(-5, 5_000) == 0


async def test_explicit_provider_rate_price_overrides_the_flat_table(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    set_org_context(session, org.id)
    session.add(
        ProviderRate(
            id=uuid.uuid4(),
            org_id=org.id,
            provider="bandwidth",
            metric="sms_out",
            unit_cost_micros=4_000,
            price_micros=7_777,
        )
    )
    await session.commit()
    assert await telephony_billing.unit_price(session, org.id, "bandwidth", "sms_out") == 7_777
    # Every other metric still comes from the flat table.
    assert await telephony_billing.unit_price(session, org.id, "bandwidth", "sms_in") == 10_000


async def test_unknown_carrier_traffic_is_charged_and_gated(session):
    """Before flat pricing, unit_price returned 0 for a carrier with no rate card, so its
    traffic was neither charged nor refusable. Flat per-metric pricing removes carrier
    identity from the decision."""
    org = await _new_org(session)
    await _enable(session, org.id, balance=SMS_OUT - 1)
    with pytest.raises(TelephonyCreditsError):
        await telephony_billing.require_sms_credit(
            session, org.id, carrier="unpriced-co", segments=1, is_mms=False
        )
    message = Message(
        id=uuid.uuid4(),
        org_id=org.id,
        thread_id=uuid.uuid4(),
        direction="outbound",
        status="accepted",
        from_e164="+12145550108",
        to_e164="+19725550108",
        body="x",
        media=[],
        carrier="unpriced-co",
        segment_count_est=1,
    )
    await telephony_billing.charge_sms(session, org.id, message)
    await session.commit()
    assert await _balance(session, org.id) == (SMS_OUT - 1) - SMS_OUT == -1


# ==================================================================================
# Per-second voice billing and enforcement
# ==================================================================================
@pytest.mark.parametrize(
    "duration_seconds, expected_micros",
    [(0, 0), (1, 84), (30, 2_500), (60, 5_000), (61, 5_084), (125, 10_417)],
)
async def test_finished_call_charges_the_exact_per_second_amount(
    session, duration_seconds, expected_micros
):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    now = _now()
    set_org_context(session, org.id)
    session.add(
        _outbound_call(
            org.id,
            status="completed",
            answered_at=now - timedelta(minutes=5),
            ended_at=now - timedelta(minutes=1),
            duration_seconds=duration_seconds,
        )
    )
    await session.commit()

    assert await telephony_billing.bill_finished_calls(session) == 1
    charged = [-row.amount_micros for row in await _usage(session, org.id)]
    assert charged == ([] if expected_micros == 0 else [expected_micros])
    assert await _balance(session, org.id) == 1_000_000 - expected_micros


async def test_outbound_dial_is_refused_below_one_minute_of_credit(session):
    """Hard stop: the floor to DIAL is a whole minute's worth of credit, even though
    billing itself is per-second. One second's worth (84 micros) is not enough - a call
    that connects and is cut off a second later bills for something the customer could not
    use."""
    org = await _new_org(session)
    await _enable(session, org.id, balance=84)  # exactly one second of talk time
    with pytest.raises(TelephonyCreditsError) as excinfo:
        await telephony_billing.require_call_credit(session, org.id, _outbound_call(org.id))
    assert excinfo.value.http_status == 402
    assert excinfo.value.code == "insufficient_credits"
    # Nothing was held: a refused dial must not touch the ledger.
    assert await _balance(session, org.id) == 84


async def test_outbound_dial_is_refused_one_micro_below_a_minute(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=VOICE_MIN - 1)  # 4_999 micros
    with pytest.raises(TelephonyCreditsError) as excinfo:
        await telephony_billing.require_call_credit(session, org.id, _outbound_call(org.id))
    assert excinfo.value.http_status == 402
    assert excinfo.value.code == "insufficient_credits"


async def test_outbound_dial_is_allowed_at_exactly_one_minute_of_credit(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=VOICE_MIN)  # 5_000 micros
    await telephony_billing.require_call_credit(session, org.id, _outbound_call(org.id))
    await session.commit()
    # The hold is capped at what the org actually has, so the whole balance is held.
    assert await _balance(session, org.id) == 0


async def test_billing_stays_per_second_despite_the_one_minute_dial_floor(session):
    """The floor gates the DIAL; it never rounds the CHARGE up to a minute."""
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    now = _now()
    call = _outbound_call(
        org.id,
        status="completed",
        answered_at=now - timedelta(minutes=2),
        ended_at=now - timedelta(minutes=1),
        duration_seconds=10,
    )
    await telephony_billing.require_call_credit(session, org.id, call)
    session.add(call)
    await session.commit()

    assert await telephony_billing.bill_finished_calls(session) == 1
    # 10s x $0.005/min = (10 * 5_000 + 59) // 60 = 834 micros, NOT a full minute's 5_000.
    assert [-row.amount_micros for row in await _usage(session, org.id)] == [834]
    assert await _balance(session, org.id) == 1_000_000 - 834


async def test_outbound_sms_is_refused_one_micro_below_the_segment_price(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=SMS_OUT - 1)
    with pytest.raises(TelephonyCreditsError):
        await telephony_billing.require_sms_credit(
            session, org.id, carrier="bandwidth", segments=1, is_mms=False
        )


async def test_outbound_sms_is_allowed_at_exactly_the_segment_price(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=SMS_OUT)
    await telephony_billing.require_sms_credit(
        session, org.id, carrier="bandwidth", segments=1, is_mms=False
    )  # must not raise


async def test_three_segment_sms_is_refused_when_only_two_segments_are_funded(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=3 * SMS_OUT - 1)
    with pytest.raises(TelephonyCreditsError):
        await telephony_billing.require_sms_credit(
            session, org.id, carrier="bandwidth", segments=3, is_mms=False
        )
    await credits.topup(session, org.id, 1, reference="one-more-micro")
    await session.commit()
    await telephony_billing.require_sms_credit(
        session, org.id, carrier="bandwidth", segments=3, is_mms=False
    )  # 30_000 exactly covers it


async def test_number_order_is_refused_below_ten_dollars_and_allowed_at_it(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=NUMBER_MRC - 1)
    with pytest.raises(TelephonyCreditsError):
        await telephony_billing.require_number_credit(session, org.id, "bandwidth")
    await credits.topup(session, org.id, 1, reference="one-more-micro")
    await session.commit()
    await telephony_billing.require_number_credit(session, org.id, "bandwidth")  # must not raise


# ==================================================================================
# Prepaid by default (migration 0055)
# ==================================================================================
async def test_a_new_org_is_created_with_the_prepaid_gate_on(session):
    """The suite-wide default is OFF (conftest) so pre-existing tests keep working; this
    pins the PRODUCTION default, which is ON."""
    from app import config as config_mod
    from app.repositories import orgs as orgs_repo
    from tests.conftest import make_settings

    user_id = uuid.uuid4()
    session.add(
        User(
            id=user_id,
            email=f"prepaid-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            is_active=True,
        )
    )
    await session.flush()

    previous = config_mod.get_active_settings()
    config_mod.set_active_settings(make_settings(telephony_prepaid_default=True))
    try:
        org = await orgs_repo.create_org_with_owner(
            session, name=f"Prepaid Default {uuid.uuid4().hex[:6]}", owner_id=user_id
        )
        await session.commit()
    finally:
        config_mod.set_active_settings(previous)

    assert org.telephony_prepaid is True
    # telephony_prepaid_since MUST be stamped: _billable_org_filter() requires it, so an org
    # with the gate on and no `since` would be blocked on outbound yet never billed.
    assert org.telephony_prepaid_since is not None


async def test_the_prepaid_default_can_be_switched_off_for_a_new_org(session):
    from app import config as config_mod
    from app.repositories import orgs as orgs_repo
    from tests.conftest import make_settings

    user_id = uuid.uuid4()
    session.add(
        User(
            id=user_id,
            email=f"prepaid-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            is_active=True,
        )
    )
    await session.flush()

    previous = config_mod.get_active_settings()
    config_mod.set_active_settings(make_settings(telephony_prepaid_default=False))
    try:
        org = await orgs_repo.create_org_with_owner(
            session, name=f"Prepaid Off {uuid.uuid4().hex[:6]}", owner_id=user_id
        )
        await session.commit()
    finally:
        config_mod.set_active_settings(previous)

    assert org.telephony_prepaid is False
    assert org.telephony_prepaid_since is None
