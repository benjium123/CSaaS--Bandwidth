"""Proof that the prepaid credit limit is REALLY enforced, not merely reported.

What this file proves
- While an org is funded, its outbound SMS go out: three one-segment sends each succeed and
  move the balance by exactly the org's live per-segment price.
- The send that would push the balance past zero is REFUSED with 402 and error code
  ``insufficient_credits``, before any Message row is written.
- The balance never goes below zero on the outbound path.
- A top-up restores sending immediately.
- The gate bites at two independent points: the pre-send check inside ``send_message``
  (which raises ``TelephonyCreditsError``) and the dispatch-time check ``can_send_sms``
  (which returns False so the dispatcher can reject the Message instead of raising).
- Charging the same message twice moves the balance exactly once: the ledger is idempotent
  on its reference, at both the ``charge_sms`` and the ``charge_usage`` level.
- The PLATFORM itself prices traffic: on a carrier with no rate card and no per-org override,
  the flat platform price applies and a zero-balance org is refused.

IMPORTANT: the loopback carrier is a SIMULATION. ``LoopbackCarrier`` implements MESSAGING
and has no ``place_call``, so there is no end-to-end voice path here at all. The VOICE path
is therefore covered at SERVICE level only, by calling
``telephony_billing.require_call_credit`` directly. No real carrier, no PSTN and no real
money are involved anywhere in this file.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Call, CreditLedgerEntry
from app.services import credits, messaging, telephony_billing
from tests.conftest import auth_headers, make_org_with_number
from tests.e2e_billing_helpers import (
    balance,
    enable_prepaid,
    messages_of,
    price_of,
    seed_rates,
    usage_entries,
)


async def test_credit_limit_stops_sending_and_a_topup_restores_it(app_with_loopback, session):
    client, _carrier, _app = app_with_loopback
    ext = "+14695551101"

    token, org, _number = await make_org_with_number(
        client, "credit-enforce-1@example.com", "Credit Org 1", "+12145551101"
    )
    org_uuid = uuid.UUID(str(org["id"]))
    h = auth_headers(token, org["id"])

    await seed_rates(session, org_uuid)
    await enable_prepaid(session, org_uuid, balance_micros=0)
    per_sms = await price_of(session, org_uuid, "sms_out")

    await credits.topup(session, org_uuid, 3 * per_sms, reference=f"topup-{uuid.uuid4()}")
    await session.commit()
    assert await balance(session, org_uuid) == 3 * per_sms

    # NO carrier.drain() anywhere in this test: the loopback echo would ingest an inbound
    # reply and charge sms_in too, which would make the balance arithmetic non-deterministic.
    # Here we are measuring the OUTBOUND gate only.
    for n, body in enumerate(("one", "two", "three"), start=1):
        resp = await client.post(
            "/api/v1/messages", json={"to": ext, "body": body}, headers=h
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "accepted"
        assert await balance(session, org_uuid) == (3 - n) * per_sms

    denied = await client.post(
        "/api/v1/messages", json={"to": ext, "body": "four"}, headers=h
    )
    assert denied.status_code == 402, denied.text
    assert denied.json()["error"]["code"] == "insufficient_credits"

    bal = await balance(session, org_uuid)
    assert bal == 0
    assert bal >= 0

    bodies = [m.body for m in await messages_of(session, org_uuid)]
    assert "four" not in bodies, bodies

    entries = await usage_entries(session, org_uuid)
    assert len(entries) == 3, [(e.reference, e.amount_micros) for e in entries]
    assert all(e.amount_micros == -per_sms for e in entries), [
        e.amount_micros for e in entries
    ]

    await credits.topup(session, org_uuid, 2 * per_sms, reference=f"topup-{uuid.uuid4()}")
    await session.commit()

    restored = await client.post(
        "/api/v1/messages", json={"to": ext, "body": "five"}, headers=h
    )
    assert restored.status_code == 201, restored.text
    assert restored.json()["status"] == "accepted"
    assert await balance(session, org_uuid) == per_sms


async def test_dispatch_time_gate_rejects_when_credit_vanishes_mid_flight(
    app_with_loopback, session
):
    client, carrier, _app = app_with_loopback
    ext = "+14695551102"

    _token, org, number = await make_org_with_number(
        client, "credit-enforce-2@example.com", "Credit Org 2", "+12145551102"
    )
    org_uuid = uuid.UUID(str(org["id"]))

    await seed_rates(session, org_uuid)
    per_sms = await price_of(session, org_uuid, "sms_out")
    await enable_prepaid(session, org_uuid, balance_micros=per_sms)

    message = await messaging.send_message(
        session,
        org_uuid,
        carrier,
        to_e164=ext,
        from_e164=number["e164"],
        body="dispatch gate",
    )
    assert message.status == "accepted"
    assert await balance(session, org_uuid) == 0

    with pytest.raises(telephony_billing.TelephonyCreditsError):
        await messaging.send_message(
            session,
            org_uuid,
            carrier,
            to_e164=ext,
            from_e164=number["e164"],
            body="over budget",
        )

    # The dispatch-time gate is a separate seam from the pre-send check above. Building the
    # in-flight Message exactly the way send_message does is fiddly, so this is a SERVICE
    # level assertion on can_send_sms itself - honest about that and nothing more.
    assert await telephony_billing.can_send_sms(session, org_uuid, message) is False

    await credits.topup(session, org_uuid, 10 * per_sms, reference=f"topup-{uuid.uuid4()}")
    await session.commit()
    assert await balance(session, org_uuid) == 10 * per_sms
    assert await telephony_billing.can_send_sms(session, org_uuid, message) is True


async def test_voice_credit_gate_refuses_at_zero_and_holds_when_funded(
    app_with_loopback, session
):
    """Voice is checked at SERVICE level only: ``LoopbackCarrier`` implements messaging and
    has no ``place_call``, so there is no end-to-end voice path to exercise here."""
    client, _carrier, _app = app_with_loopback

    _t1, org1, _n1 = await make_org_with_number(
        client, "credit-enforce-3a@example.com", "Credit Org 3A", "+12145551103"
    )
    org1_uuid = uuid.UUID(str(org1["id"]))
    await seed_rates(session, org1_uuid)
    await enable_prepaid(session, org1_uuid, balance_micros=0)

    call1 = Call(
        id=uuid.uuid4(),
        org_id=org1_uuid,
        direction="outbound",
        contact_e164="+14695551103",
        our_e164="+12145551103",
        carrier="loopback",
        status="queued",
        extra={},
    )

    with pytest.raises(telephony_billing.TelephonyCreditsError):
        await telephony_billing.require_call_credit(session, org1_uuid, call1)

    assert await balance(session, org1_uuid) == 0
    set_org_context(session, org1_uuid)
    reserves_a = (
        await session.execute(
            sa.select(CreditLedgerEntry).where(CreditLedgerEntry.entry_type == "reserve")
        )
    ).scalars().all()
    assert list(reserves_a) == [], list(reserves_a)

    _t2, org2, _n2 = await make_org_with_number(
        client, "credit-enforce-3b@example.com", "Credit Org 3B", "+19725551103"
    )
    org2_uuid = uuid.UUID(str(org2["id"]))
    await seed_rates(session, org2_uuid)
    per_minute = await price_of(session, org2_uuid, "voice_min_out")
    await enable_prepaid(session, org2_uuid, balance_micros=10 * per_minute)

    call2 = Call(
        id=uuid.uuid4(),
        org_id=org2_uuid,
        direction="outbound",
        contact_e164="+14695551105",
        our_e164="+19725551103",
        carrier="loopback",
        status="queued",
        extra={},
    )

    balance_before = await balance(session, org2_uuid)
    held = min(per_minute * telephony_billing.CALL_RESERVE_MINUTES, balance_before)
    await telephony_billing.require_call_credit(session, org2_uuid, call2)
    await session.commit()

    set_org_context(session, org2_uuid)
    reserve = (
        await session.execute(
            sa.select(CreditLedgerEntry).where(
                CreditLedgerEntry.entry_type == "reserve",
                CreditLedgerEntry.reference
                == telephony_billing.call_hold_reference(call2.id),
            )
        )
    ).scalar_one()
    assert reserve.amount_micros == -held
    assert await balance(session, org2_uuid) == balance_before - held


async def test_charging_the_same_message_twice_moves_the_balance_once(
    app_with_loopback, session
):
    client, _carrier, _app = app_with_loopback
    ext = "+14695551104"

    token, org, _number = await make_org_with_number(
        client, "credit-enforce-4@example.com", "Credit Org 4", "+12145551104"
    )
    org_uuid = uuid.UUID(str(org["id"]))
    h = auth_headers(token, org["id"])

    await seed_rates(session, org_uuid)
    per_sms = await price_of(session, org_uuid, "sms_out")
    await enable_prepaid(session, org_uuid, balance_micros=100 * per_sms)

    sent = await client.post(
        "/api/v1/messages", json={"to": ext, "body": "charge me once"}, headers=h
    )
    assert sent.status_code == 201, sent.text

    message = next(
        m for m in await messages_of(session, org_uuid) if m.direction == "outbound"
    )
    ref = f"sms:{message.id}"

    before = await balance(session, org_uuid)
    await telephony_billing.charge_sms(session, org_uuid, message)
    await session.commit()
    assert await balance(session, org_uuid) == before

    entries = [e for e in await usage_entries(session, org_uuid) if e.reference == ref]
    assert len(entries) == 1, [e.reference for e in entries]

    # The ledger primitive underneath is idempotent on its reference too - a third charge
    # with the same reference writes nothing at all.
    await credits.charge_usage(session, org_uuid, per_sms, reference=ref)
    await session.commit()
    assert await balance(session, org_uuid) == before

    entries = [e for e in await usage_entries(session, org_uuid) if e.reference == ref]
    assert len(entries) == 1, [e.reference for e in entries]


async def test_platform_prices_traffic_on_a_carrier_with_no_rate_card(
    app_with_loopback, session
):
    """The ONE test that must fail if the platform ever prices traffic at zero.

    It deliberately does NOT call ``seed_rates``: seeding a per-org ``ProviderRate``
    override would mask exactly the condition under test. "loopback" is a carrier with NO
    entry in ``app.models.spend.DEFAULT_RATES_MICROS``, so asserting its price is > 0 proves
    the flat platform price applies to an unrated carrier rather than traffic being free.
    """
    client, _carrier, _app = app_with_loopback
    ext = "+14695551106"

    token, org, _number = await make_org_with_number(
        client, "credit-enforce-5@example.com", "Credit Org 5", "+12145551105"
    )
    org_uuid = uuid.UUID(str(org["id"]))
    h = auth_headers(token, org["id"])

    await enable_prepaid(session, org_uuid, balance_micros=0)

    for metric in ("sms_out", "sms_in", "voice_min_out"):
        price = await telephony_billing.unit_price(session, org_uuid, "loopback", metric)
        assert price > 0, (
            f"platform priced {metric} at {price} on an unrated carrier; traffic would be free"
        )

    refused = await client.post(
        "/api/v1/messages", json={"to": ext, "body": "no credit"}, headers=h
    )
    assert refused.status_code == 402, refused.text
    assert refused.json()["error"]["code"] == "insufficient_credits"
