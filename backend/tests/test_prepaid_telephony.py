"""Prepaid telephony hard gate (migration 0041, services/telephony_billing.py).

Prices below are the bandwidth rate card (FakeCarrier.name == "bandwidth") x the 30%
default traffic markup, rounded up - the same arithmetic telephony_billing.unit_price does.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Call, CreditLedgerEntry, Message, Org, OrgNumber
from app.services import credits, telephony_billing
from app.services import messaging as messaging_svc
from app.services.telephony_billing import TelephonyCreditsError
from tests.conftest import TEST_PLATFORM_OPS_TOKEN, auth_headers, make_org_with_number

SMS_OUT = 5_200  # 4_000 x 1.30
SMS_IN = 5_200  # 4_000 x 1.30
MIN_OUT = 13_000  # 10_000 x 1.30
MIN_IN = 7_150  # 5_500 x 1.30
NUMBER_MRC = 455_000  # 350_000 x 1.30

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
        duration_seconds=121,  # 3 billable minutes
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
    assert await _balance(session, org.id) == 1_000_000 - 3 * MIN_OUT, (
        "hold released, minutes charged"
    )
    set_org_context(session, org.id)
    assert (await session.get(Call, call.id)).billed_at is not None
    assert (await session.get(Call, old.id)).billed_at is None

    assert await telephony_billing.bill_finished_calls(session) == 0
    assert await _balance(session, org.id) == 1_000_000 - 3 * MIN_OUT


async def test_inbound_call_minutes_are_charged_without_a_gate(session):
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
    assert await _balance(session, org.id) == -MIN_IN


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
    await _enable(session, org.id, balance=10_000_000)
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
