"""P44b: concurrency, daily spend ceiling, auto-recharge caps, fraud signals."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.base import set_org_context
from app.errors import PermissionDeniedError
from app.models import Call, KycProfile, Org
from app.services import credits, exposure, telephony_access


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _org(session, *, age_days: int = 90) -> Org:
    org = Org(id=uuid.uuid4(), name="Exposure Org", slug=f"ex-{uuid.uuid4().hex[:16]}")
    org.created_at = _now() - timedelta(days=age_days)
    session.add(org)
    await session.commit()
    set_org_context(session, org.id)
    return org


def _live_call(org_id, status: str = "in_progress", age: timedelta = timedelta(minutes=1)):
    return Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="outbound",
        contact_e164="+19725550100",
        our_e164="+19725550199",
        carrier="telnyx",
        status=status,
        created_at=_now() - age,
    )


async def test_sixth_concurrent_call_is_refused(session, settings):
    org = await _org(session)
    for _ in range(5):
        session.add(_live_call(org.id))
    await session.commit()
    with pytest.raises(PermissionDeniedError) as err:
        await exposure.require_call_slot(session, settings, org.id)
    assert err.value.code == "concurrent_call_limit"


async def test_finished_and_stale_calls_do_not_hold_a_slot(session, settings):
    org = await _org(session)
    for _ in range(3):
        session.add(_live_call(org.id, status="completed"))
    session.add(_live_call(org.id, age=timedelta(hours=5)))  # lost webhook
    for _ in range(4):
        session.add(_live_call(org.id))
    await session.commit()
    await exposure.require_call_slot(session, settings, org.id)  # 4 live < 5


async def test_operator_limit_raises_the_concurrency_cap(session, settings):
    org = await _org(session)
    session.add(KycProfile(id=uuid.uuid4(), org_id=org.id, limits={"max_concurrent_calls": 8}))
    for _ in range(6):
        session.add(_live_call(org.id))
    await session.commit()
    await exposure.require_call_slot(session, settings, org.id)


async def test_new_account_daily_spend_ceiling(session, settings):
    org = await _org(session, age_days=3)
    await credits.topup(session, org.id, 100_000_000, reference=f"t-{uuid.uuid4()}")
    await credits.charge_usage(session, org.id, 25_000_000, reference=f"u-{uuid.uuid4()}")
    await session.commit()
    assert await exposure.spent_today_micros(session, org.id) == 25_000_000
    assert await telephony_access.refusal(session, settings, org.id, "call") == "daily_spend_reached"
    assert await telephony_access.refusal(session, settings, org.id, "sms") == "daily_spend_reached"


async def test_established_account_has_the_higher_ceiling(session, settings):
    org = await _org(session, age_days=90)
    await credits.topup(session, org.id, 100_000_000, reference=f"t-{uuid.uuid4()}")
    await credits.charge_usage(session, org.id, 25_000_000, reference=f"u-{uuid.uuid4()}")
    await session.commit()
    assert await exposure.refusal(session, settings, org.id, "call") is None


async def test_auto_recharge_refused_for_new_account(session, settings):
    org = await _org(session, age_days=3)
    assert await exposure.auto_recharge_refusal(session, settings, org, 10_000_000) == "new_account"


async def test_auto_recharge_daily_count_and_amount_caps(session, settings):
    org = await _org(session, age_days=90)
    recent = [{"at": (_now() - timedelta(hours=h)).isoformat(), "micros": 10_000_000} for h in (1, 2, 3)]
    org.credit_auto_recharge = {"enabled": True, "history": recent}
    assert await exposure.auto_recharge_refusal(session, settings, org, 10_000_000) == "daily_count"
    org.credit_auto_recharge = {"enabled": True, "history": recent[:1]}
    assert await exposure.auto_recharge_refusal(session, settings, org, 10_000_000) is None
    assert (
        await exposure.auto_recharge_refusal(session, settings, org, 295_000_000) == "daily_amount"
    )


def test_recharge_history_is_pruned_to_one_day():
    old = (_now() - timedelta(days=2)).isoformat()
    new = (_now() - timedelta(hours=2)).isoformat()
    kept = exposure.recharges_in_last_day(
        {"history": [{"at": old, "micros": 1}, {"at": new, "micros": 2}, "junk"]}
    )
    assert [e["micros"] for e in kept] == [2]
