"""Bundle units do not roll over: what is left at the monthly cycle renewal is written off."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import BundleLedgerEntry, Org
from app.services import bundles, telephony_billing

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


async def _org(session, *, plan_started_at=None, created_at=None) -> Org:
    org = Org(id=uuid.uuid4(), name="Expiry Co", slug=f"expiry-{uuid.uuid4().hex[:16]}")
    org.plan_started_at = plan_started_at
    if created_at is not None:
        org.created_at = created_at
    session.add(org)
    await session.commit()
    return org


async def _buy(session, org_id, kind, qty, at: datetime) -> None:
    row = await bundles.credit(session, org_id, kind, qty, reference=f"buy-{uuid.uuid4()}")
    row.created_at = at
    await session.commit()


async def _rows(session, org_id, entry_type):
    set_org_context(session, org_id)
    return (
        await session.execute(
            sa.select(BundleLedgerEntry).where(BundleLedgerEntry.entry_type == entry_type)
        )
    ).scalars().all()


async def test_last_cycle_units_expire_and_this_cycles_are_kept(session):
    # Plan renews on the 10th; "now" is the 12th, so the cycle started Sep 10.
    org = await _org(session, plan_started_at=datetime(2026, 6, 10, tzinfo=timezone.utc))
    await _buy(session, org.id, "sms", 1_000, datetime(2026, 9, 5, tzinfo=timezone.utc))
    await bundles.take(session, org.id, "sms", 300, reference="use-1")
    await session.commit()
    await _buy(session, org.id, "sms", 1_000, datetime(2026, 9, 11, tzinfo=timezone.utc))
    await _buy(session, org.id, "voice", 1_000, datetime(2026, 8, 20, tzinfo=timezone.utc))

    assert await bundles.expire_unused(session, now=NOW) == 2
    assert await bundles.units(session, org.id, "sms") == 1_000
    assert await bundles.units(session, org.id, "voice") == 0
    [sms_expired] = [r for r in await _rows(session, org.id, "expire") if r.kind == "sms"]
    assert sms_expired.delta_units == -700
    assert sms_expired.reference == "expire:2026-09-10"

    # A second pass in the same cycle writes nothing, even after more usage.
    await bundles.take(session, org.id, "sms", 100, reference="use-2")
    await session.commit()
    assert await bundles.expire_unused(session, now=NOW) == 0
    assert await bundles.units(session, org.id, "sms") == 900


async def test_nothing_expires_before_the_renewal(session):
    org = await _org(session, plan_started_at=datetime(2026, 6, 10, tzinfo=timezone.utc))
    await _buy(session, org.id, "mms", 100, datetime(2026, 9, 10, 0, 5, tzinfo=timezone.utc))
    assert await bundles.expire_unused(session, now=NOW) == 0
    assert await bundles.units(session, org.id, "mms") == 100


async def test_a_workspace_without_a_plan_renews_on_its_signup_day(session):
    org = await _org(session, created_at=datetime(2026, 7, 3, tzinfo=timezone.utc))
    await _buy(session, org.id, "sms", 1_000, datetime(2026, 9, 2, tzinfo=timezone.utc))
    await _buy(session, org.id, "sms", 1_000, datetime(2026, 9, 4, tzinfo=timezone.utc))
    assert await bundles.expire_unused(session, now=NOW) == 1
    assert await bundles.units(session, org.id, "sms") == 1_000


async def test_the_sweeper_expires_bundles_every_fifteen_minutes(session, monkeypatch):
    calls = []

    async def fake_expire(_session, *, now=None):
        calls.append(now)
        return 0

    async def zero(*_a, **_k):
        return 0

    monkeypatch.setattr(bundles, "expire_unused", fake_expire)
    monkeypatch.setattr(telephony_billing, "bill_finished_calls", zero)
    monkeypatch.setattr(telephony_billing, "enforce_active_calls", zero)
    monkeypatch.setattr(telephony_billing, "renew_number_rentals", zero)
    monkeypatch.setattr(telephony_billing, "_bundle_expiry_last_run", None)
    clock = [1_000.0]
    monkeypatch.setattr(telephony_billing.time, "monotonic", lambda: clock[0])

    out = await telephony_billing.telephony_tick(session, hangup=None, now=NOW)
    assert out["bundle_expiries"] == 0
    clock[0] += 14 * 60
    assert "bundle_expiries" not in await telephony_billing.telephony_tick(
        session, hangup=None, now=NOW
    )
    clock[0] += 60
    await telephony_billing.telephony_tick(session, hangup=None, now=NOW)
    assert len(calls) == 2
    assert telephony_billing.BUNDLE_EXPIRY_INTERVAL_SECONDS == 15 * 60
