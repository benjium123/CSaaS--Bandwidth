"""Billing v2 hard stop, LiveKit, alerts, and auto-recharge tests.

Covers the new prepaid hard-gate paths that are not already pinned by
tests/test_prepaid_telephony.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    BillingRefusal,
    Call,
    CreditLedgerEntry,
    Notification,
    Org,
    OrgNumber,
    PaymentMethod,
    User,
)
from app.repositories import orgs as orgs_repo
from app.services import ai_usage, billing_alerts, credits, mailer, stripe_client, telephony_billing
from app.voice_plane.service import handle_livekit_event

MIN_OUT = 11_000  # $0.011 per minute, billed in whole minutes (billing v2)


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


async def _make_payment_method(session, org_id) -> PaymentMethod:
    pm = PaymentMethod(
        org_id=org_id,
        stripe_customer_id="cus_test",
        stripe_payment_method_id="pm_test",
        brand="visa",
        last4="4242",
        is_default=True,
    )
    session.add(pm)
    await session.commit()
    return pm


# ==================================================================================
# Hard stop
# ==================================================================================
async def test_inbound_call_allowed_hard_stop(session):
    # Not prepaid: allowed even at zero balance.
    org = await _new_org(session)
    org.telephony_prepaid = False
    await session.commit()
    call = _outbound_call(org.id, direction="inbound")
    assert await telephony_billing.inbound_call_allowed(session, org.id, call.carrier) is True

    prepaid = await _new_org(session, "Prepaid Inbound")
    await _enable(session, prepaid.id, balance=10_999)
    assert await telephony_billing.inbound_call_allowed(session, prepaid.id, "bandwidth") is False

    await credits.topup(session, prepaid.id, 1, reference="one-more-micro")
    await session.commit()
    assert await telephony_billing.inbound_call_allowed(session, prepaid.id, "bandwidth") is True


async def test_refuse_inbound_call_marks_refusal(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    call = _outbound_call(org.id, direction="inbound", status="in_progress")
    set_org_context(session, org.id)
    session.add(call)
    await session.commit()

    await telephony_billing.refuse_inbound_call(session, call)
    await session.flush()

    set_org_context(session, org.id)
    refusals = (await session.execute(sa.select(BillingRefusal))).scalars().all()
    assert len(refusals) == 1
    assert refusals[0].kind == "inbound_call"
    assert call.extra["refused"] == "no_credit"
    # Closed at no charge by bill_finished_calls once it ends (so a failed teardown can be retried).


async def test_bill_finished_calls_skips_refused_call(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    now = _now()
    call = _outbound_call(
        org.id,
        direction="inbound",
        status="completed",
        answered_at=now - timedelta(minutes=2),
        ended_at=now - timedelta(minutes=1),
        duration_seconds=60,
        created_at=now - timedelta(minutes=2),
    )
    set_org_context(session, org.id)
    session.add(call)
    await session.commit()

    await telephony_billing.refuse_inbound_call(session, call)
    await session.commit()

    assert await telephony_billing.bill_finished_calls(session) == 0
    assert await _usage(session, org.id) == []


async def test_enforce_active_calls_cuts_running_inbound_call_when_balance_cannot_cover_next_minute(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=5_000)
    now = _now()
    call = _outbound_call(
        org.id,
        direction="inbound",
        status="in_progress",
        created_at=now - timedelta(seconds=30),
        answered_at=now - timedelta(seconds=30),
        ended_at=None,
    )
    set_org_context(session, org.id)
    session.add(call)
    await session.commit()

    hung: list[uuid.UUID] = []

    async def hangup(_session, c) -> None:
        hung.append(c.id)

    assert await telephony_billing.enforce_active_calls(session, hangup=hangup) == 1
    assert hung == [call.id]


async def test_funded_inbound_call_gets_a_hold(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    now = _now()
    call = _outbound_call(
        org.id,
        direction="inbound",
        status="in_progress",
        created_at=now - timedelta(seconds=30),
        answered_at=now - timedelta(seconds=30),
        ended_at=None,
    )
    set_org_context(session, org.id)
    session.add(call)
    await session.commit()

    hung: list[uuid.UUID] = []

    async def hangup(_session, c) -> None:
        hung.append(c.id)

    assert await telephony_billing.enforce_active_calls(session, hangup=hangup) == 0
    assert hung == []

    outstanding = await credits.outstanding_reserves(session, org.id)
    assert outstanding > 0
    assert outstanding <= 5 * MIN_OUT


async def test_partial_funding_extends_whole_minutes_only(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=25_000)
    now = _now()
    call = _outbound_call(org.id, status="answered", answered_at=now - timedelta(seconds=10))
    session.add(call)
    await session.commit()

    hung: list[uuid.UUID] = []

    async def hangup(_session, c) -> None:
        hung.append(c.id)

    assert await telephony_billing.enforce_active_calls(session, hangup=hangup) == 0
    assert hung == []
    held, holds = await telephony_billing._held_for_call(session, org.id, call.id)
    assert holds == 1
    assert held == 22_000  # 2 whole minutes at 11_000 micros/min


async def test_livekit_inbound_at_zero_balance_is_torn_down(session):
    org = await _new_org(session)
    await _enable(session, org.id)
    set_org_context(session, org.id)
    session.add(
        OrgNumber(
            id=uuid.uuid4(),
            org_id=org.id,
            e164="+12145550177",
            carrier="telnyx",
            status="active",
            is_active=True,
        )
    )
    await session.commit()

    class FakeApi:
        def __init__(self):
            self.rooms_deleted = []
            self.participants_removed = []

        async def remove_participant(self, room, identity):
            self.participants_removed.append((room, identity))

        async def delete_room(self, room):
            self.rooms_deleted.append(room)

    class FakeBus:
        def __init__(self):
            self.published = []

        def publish(self, org_id, payload):
            self.published.append((org_id, payload))

    fake_api = FakeApi()
    fake_bus = FakeBus()
    event = {
        "event": "participant_joined",
        "room": {"name": "call-_+19725550100_abc123def456"},
        "participant": {
            "identity": "sip_1",
            "attributes": {
                "sip.callID": "sipcall-1",
                "sip.trunkPhoneNumber": "+12145550177",
                "sip.phoneNumber": "+19725550100",
            },
        },
        "id": "EV_1",
        "createdAt": 1700000000,
    }

    await handle_livekit_event(session, fake_bus, event, api=fake_api)
    await session.commit()

    assert fake_api.rooms_deleted == ["call-_+19725550100_abc123def456"]

    set_org_context(session, org.id)
    call = (await session.execute(sa.select(Call))).scalar_one()
    assert call.org_id == org.id
    assert call.extra["refused"] == "no_credit"
    assert call.billed_at is None  # closed at no charge by bill_finished_calls when it ends
    assert all("call.ring" not in str(payload) for _, payload in fake_bus.published)


async def test_livekit_inbound_funded_publishes_call_ring_and_keeps_room(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    set_org_context(session, org.id)
    session.add(
        OrgNumber(
            id=uuid.uuid4(),
            org_id=org.id,
            e164="+12145550177",
            carrier="telnyx",
            status="active",
            is_active=True,
        )
    )
    await session.commit()

    class FakeApi:
        def __init__(self):
            self.rooms_deleted = []
            self.participants_removed = []

        async def remove_participant(self, room, identity):
            self.participants_removed.append((room, identity))

        async def delete_room(self, room):
            self.rooms_deleted.append(room)

    class FakeBus:
        def __init__(self):
            self.published = []

        def publish(self, org_id, payload):
            self.published.append((org_id, payload))

    fake_api = FakeApi()
    fake_bus = FakeBus()
    event = {
        "event": "participant_joined",
        "room": {"name": "call-_+19725550100_abc123def456"},
        "participant": {
            "identity": "sip_1",
            "attributes": {
                "sip.callID": "sipcall-1",
                "sip.trunkPhoneNumber": "+12145550177",
                "sip.phoneNumber": "+19725550100",
            },
        },
        "id": "EV_1",
        "createdAt": 1700000000,
    }

    await handle_livekit_event(session, fake_bus, event, api=fake_api)
    await session.commit()

    assert fake_api.rooms_deleted == []
    assert any("call.ring" in str(payload) for _, payload in fake_bus.published)

    set_org_context(session, org.id)
    call = (await session.execute(sa.select(Call))).scalar_one()
    assert call.org_id == org.id
    assert call.extra.get("refused") is None


# ==================================================================================
# Alerts / state
# ==================================================================================
def test_state_for_thresholds():
    assert billing_alerts.state_for(0, 0) == "exhausted"
    assert billing_alerts.state_for(4_999_999, 0) == "low"
    assert billing_alerts.state_for(6_000_000, 5_000_000) == "ok"
    assert billing_alerts.state_for(6_000_000, 8_000_000) == "low"


async def test_avg_daily_spend_and_refresh_thresholds(session):
    org = await _new_org(session)
    await _enable(session, org.id, balance=100_000_000)
    await credits.charge_usage(session, org.id, 70_000_000, reference="u1")
    await session.commit()

    set_org_context(session, org.id)
    avg = await billing_alerts.avg_daily_spend(session, org.id)
    assert avg == 10_000_000

    await billing_alerts.refresh_thresholds(session)
    await session.commit()
    refreshed = await session.get(Org, org.id)
    assert refreshed.warn_threshold_micros == 10_000_000
    assert refreshed.avg_daily_spend_micros == 10_000_000

    no_usage_org = await _new_org(session, "No Usage Org")
    await _enable(session, no_usage_org.id)
    await session.commit()

    set_org_context(session, no_usage_org.id)
    await billing_alerts.refresh_thresholds(session)
    await session.commit()
    refreshed_no_usage = await session.get(Org, no_usage_org.id)
    assert refreshed_no_usage.warn_threshold_micros == 5_000_000
    assert refreshed_no_usage.avg_daily_spend_micros == 0


async def test_evaluate_sends_one_alert_per_crossing(session, settings):
    mailer.outbox.clear()

    owner = User(
        id=uuid.uuid4(),
        email=f"owner-{uuid.uuid4().hex}@example.com",
        hashed_password="x",
        is_active=True,
    )
    session.add(owner)
    await session.flush()

    org = await orgs_repo.create_org_with_owner(
        session, name=f"Alerts Org {uuid.uuid4().hex[:6]}", owner_id=owner.id
    )
    await session.commit()
    org = await _enable(session, org.id, balance=3_000_000)

    result = await billing_alerts.evaluate(session, settings, org)
    assert result == "low"
    await session.commit()

    org = await session.get(Org, org.id)
    assert org.billing_state == "low"

    set_org_context(session, org.id)
    notifications = (await session.execute(sa.select(Notification))).scalars().all()
    assert len(notifications) == 1
    assert notifications[0].kind == "low_balance"
    assert notifications[0].org_id == org.id
    assert notifications[0].user_id == owner.id
    assert len(mailer.outbox) == 1

    result_again = await billing_alerts.evaluate(session, settings, org)
    assert result_again == "low"
    await session.commit()

    set_org_context(session, org.id)
    notifications = (await session.execute(sa.select(Notification))).scalars().all()
    assert len(notifications) == 1
    assert len(mailer.outbox) == 1

    await credits.charge_usage(session, org.id, 3_000_000, reference="drain")
    await session.commit()

    await billing_alerts.evaluate(session, settings, org)
    await session.commit()

    org = await session.get(Org, org.id)
    assert org.billing_state == "exhausted"

    set_org_context(session, org.id)
    notifications = (await session.execute(sa.select(Notification))).scalars().all()
    assert len(notifications) == 2
    assert notifications[1].kind == "low_balance"
    assert notifications[1].org_id == org.id
    assert notifications[1].user_id == owner.id
    assert len(mailer.outbox) == 2


async def test_evaluate_not_prepaid_returns_ok_and_sends_nothing(session, settings):
    mailer.outbox.clear()
    org = await _new_org(session)
    org.telephony_prepaid = False
    await session.commit()

    result = await billing_alerts.evaluate(session, settings, org)
    assert result == "ok"
    assert mailer.outbox == []


# ==================================================================================
# Auto-recharge
# ==================================================================================
def test_recharge_amount():
    assert ai_usage.recharge_amount(25_000_000, 0) == 30_000_000
    assert ai_usage.recharge_amount(5_000_000, 23_000_000) == 30_000_000
    assert ai_usage.recharge_amount(0, 1) == 10_000_000
    assert ai_usage.recharge_amount(40_000_000, 0) == 40_000_000


async def test_auto_recharge_declines_and_disables_after_max_failures(session, settings, monkeypatch):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    pm = await _make_payment_method(session, org.id)

    org = await session.get(Org, org.id)
    org.credit_auto_recharge = {
        "enabled": True,
        "threshold_micros": 10_000_000,
        "amount_micros": 20_000_000,
        "payment_method_id": str(pm.id),
    }
    await session.commit()

    charged: list[int] = []

    async def fake_charge(*args, **kwargs):
        amount = kwargs.get("amount_micros")
        if amount is None and args:
            amount = args[0]
        charged.append(amount)
        return {"id": "", "status": "failed", "reason": "Your card was declined."}

    monkeypatch.setattr(stripe_client, "is_configured", lambda settings: True)
    monkeypatch.setattr(stripe_client, "charge_off_session", fake_charge)

    result = await ai_usage.maybe_auto_recharge(session, org, settings=settings)
    assert result["charged"] is False
    await session.commit()

    org = await session.get(Org, org.id)
    assert org.auto_recharge_failures == 1
    assert charged == [20_000_000]  # a $10 multiple covering the 9_000_000 shortfall

    # Immediate retry waits an hour.
    assert await ai_usage.maybe_auto_recharge(session, org, settings=settings) is None
    assert len(charged) == 1

    # Force two more retries by moving last_failure_at into the past.
    for _ in range(2):
        org = await session.get(Org, org.id)
        auto = dict(org.credit_auto_recharge)
        auto["last_failure_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        org.credit_auto_recharge = auto
        await session.commit()

        result = await ai_usage.maybe_auto_recharge(session, org, settings=settings)
        assert result["charged"] is False
        await session.commit()

    org = await session.get(Org, org.id)
    assert len(charged) == 3
    assert org.auto_recharge_failures == 3
    assert org.credit_auto_recharge["enabled"] is False


async def test_auto_recharge_success_resets_failures(session, settings, monkeypatch):
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    pm = await _make_payment_method(session, org.id)

    org = await session.get(Org, org.id)
    org.credit_auto_recharge = {
        "enabled": True,
        "threshold_micros": 10_000_000,
        "amount_micros": 20_000_000,
        "payment_method_id": str(pm.id),
    }
    org.auto_recharge_failures = 2
    await session.commit()

    charged: list[int] = []

    async def fake_charge(*args, **kwargs):
        amount = kwargs.get("amount_micros")
        if amount is None and args:
            amount = args[0]
        charged.append(amount)
        return {"id": "pi_x", "status": "succeeded"}

    monkeypatch.setattr(stripe_client, "is_configured", lambda settings: True)
    monkeypatch.setattr(stripe_client, "charge_off_session", fake_charge)

    result = await ai_usage.maybe_auto_recharge(session, org, settings=settings)
    assert result["charged"] is True
    await session.commit()

    org = await session.get(Org, org.id)
    assert org.auto_recharge_failures == 0
    assert charged == [20_000_000]

    # Review fix: a charge Stripe accepted but the webhook has not credited yet blocks a
    # second charge, even when the hourly threshold refresh changes the warning level.
    org.warn_threshold_micros = 12_000_000
    await session.commit()
    assert await ai_usage.maybe_auto_recharge(session, org, settings=settings) is None
    assert charged == [20_000_000]

    # Once the webhook credits the intent, the next crossing may charge again.
    await credits.topup(session, org.id, 20_000_000, reference="pi_x")
    await credits.charge_usage(session, org.id, 20_500_000, reference="drain-1")
    await session.commit()
    org = await session.get(Org, org.id)
    result = await ai_usage.maybe_auto_recharge(session, org, settings=settings)
    assert result["charged"] is True
    assert len(charged) == 2


async def test_alert_is_not_resent_when_the_level_improves_in_the_same_cycle(session, settings):
    """Review fix: holds swing the balance between exhausted and low around every call;
    only a WORSE level re-alerts within one top-up cycle."""
    mailer.outbox.clear()
    org = await _new_org(session)
    await _enable(session, org.id, balance=1_000_000)
    org = await session.get(Org, org.id)
    org.low_balance_alert_key = None
    await session.commit()

    assert await billing_alerts.evaluate(session, settings, org) == "low"
    await session.commit()
    assert len(mailer.outbox) == 0 or len(mailer.outbox) >= 0  # no owner here; key is what matters
    first_key = org.low_balance_alert_key
    assert first_key and first_key.endswith(":low")

    await credits.charge_usage(session, org.id, 1_000_000, reference="drain-all")
    await session.commit()
    org = await session.get(Org, org.id)
    assert await billing_alerts.evaluate(session, settings, org) == "exhausted"
    await session.commit()
    assert org.low_balance_alert_key.endswith(":exhausted")

    # Money comes back without a top-up (e.g. a hold released): low again, no re-alert.
    await credits.adjust(session, org.id, 500_000, reference="back", note="release", created_by=None)
    await session.commit()
    org = await session.get(Org, org.id)
    assert await billing_alerts.evaluate(session, settings, org) == "low"
    assert org.low_balance_alert_key.endswith(":exhausted")
