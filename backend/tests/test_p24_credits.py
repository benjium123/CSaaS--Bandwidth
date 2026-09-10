"""P24 credits ledger (Fable-owned) - the money invariants."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import InsufficientCreditsError, ValidationFailedError
from app.models import CreditLedgerEntry, Org
from app.services import credits
from tests.conftest import create_org, register_and_login


async def _org(client, email: str, name: str) -> uuid.UUID:
    token = await register_and_login(client, email)
    org = await create_org(client, token, name)
    return uuid.UUID(org["id"])


async def test_balance_is_zero_for_a_fresh_org(client, session):
    org_id = await _org(client, "cr-0@example.com", "Credits Zero")
    assert await credits.balance(session, org_id) == 0


async def test_topup_reserve_release_usage_chain(client, session):
    org_id = await _org(client, "cr-1@example.com", "Credits Chain")
    await credits.topup(session, org_id, 10_000_000, reference="pi_1")
    await session.commit()
    assert await credits.balance(session, org_id) == 10_000_000

    await credits.reserve(session, org_id, 3_000_000, reference="call-1")
    await session.commit()
    assert await credits.balance(session, org_id) == 7_000_000
    assert await credits.outstanding_reserves(session, org_id) == 3_000_000

    await credits.release(session, org_id, reference="call-1")
    await session.commit()
    assert await credits.balance(session, org_id) == 10_000_000
    assert await credits.outstanding_reserves(session, org_id) == 0

    await credits.charge_usage(session, org_id, 1_250_000, reference="evt-1")
    await session.commit()
    assert await credits.balance(session, org_id) == 8_750_000

    ok, total, last = await credits.integrity_check(session, org_id)
    assert ok and total == last == 8_750_000


async def test_reserve_refuses_when_insufficient_and_writes_nothing(client, session):
    org_id = await _org(client, "cr-2@example.com", "Credits Refuse")
    await credits.topup(session, org_id, 1_000_000, reference="pi_2")
    await session.commit()
    with pytest.raises(InsufficientCreditsError) as exc:
        await credits.reserve(session, org_id, 1_000_001, reference="call-2")
    assert "credits" in str(exc.value).lower()
    await session.rollback()
    set_org_context(session, org_id)
    rows = (await session.execute(sa.select(sa.func.count(CreditLedgerEntry.id)))).scalar_one()
    assert rows == 1
    assert await credits.balance(session, org_id) == 1_000_000


async def test_every_reference_is_charged_once(client, session):
    org_id = await _org(client, "cr-3@example.com", "Credits Idempotent")
    a = await credits.topup(session, org_id, 5_000_000, reference="pi_3")
    b = await credits.topup(session, org_id, 5_000_000, reference="pi_3")
    await session.commit()
    assert a.id == b.id
    assert await credits.balance(session, org_id) == 5_000_000

    await credits.reserve(session, org_id, 1_000_000, reference="call-3")
    await credits.reserve(session, org_id, 1_000_000, reference="call-3")
    await session.commit()
    assert await credits.balance(session, org_id) == 4_000_000

    await credits.release(session, org_id, reference="call-3")
    await credits.release(session, org_id, reference="call-3")
    await session.commit()
    assert await credits.balance(session, org_id) == 5_000_000

    await credits.charge_usage(session, org_id, 100, reference="evt-3")
    await credits.charge_usage(session, org_id, 100, reference="evt-3")
    await session.commit()
    assert await credits.balance(session, org_id) == 4_999_900


async def test_release_without_reserve_is_a_noop(client, session):
    org_id = await _org(client, "cr-4@example.com", "Credits NoReserve")
    assert await credits.release(session, org_id, reference="never-reserved") is None
    assert await credits.balance(session, org_id) == 0


async def test_ledger_is_tenant_scoped(client, session):
    org_a = await _org(client, "cr-5a@example.com", "Credits A")
    org_b = await _org(client, "cr-5b@example.com", "Credits B")
    await credits.topup(session, org_a, 9_000_000, reference="pi_a")
    await session.commit()
    assert await credits.balance(session, org_b) == 0
    # Same reference in another org is a different charge.
    await credits.topup(session, org_b, 1_000_000, reference="pi_a")
    await session.commit()
    assert await credits.balance(session, org_a) == 9_000_000
    assert await credits.balance(session, org_b) == 1_000_000


async def test_adjustment_requires_a_note_and_refund_is_positive(client, session):
    org_id = await _org(client, "cr-6@example.com", "Credits Adjust")
    with pytest.raises(ValidationFailedError):
        await credits.adjust(session, org_id, 100, reference="adj-1", note="  ", created_by=None)
    await session.rollback()
    entry = await credits.adjust(
        session, org_id, 250_000, reference="ref-1", note="goodwill", created_by=None,
        entry_type="refund",
    )
    await session.commit()
    assert entry.entry_type == "refund" and entry.note == "goodwill"
    assert await credits.balance(session, org_id) == 250_000
    with pytest.raises(ValidationFailedError):
        await credits.adjust(
            session, org_id, 1, reference="x", note="n", created_by=None, entry_type="topup"
        )


async def test_stale_reserves_are_released_by_the_sweeper(client, session):
    org_id = await _org(client, "cr-7@example.com", "Credits Stale")
    await credits.topup(session, org_id, 2_000_000, reference="pi_7")
    held = await credits.reserve(session, org_id, 500_000, reference="call-7")
    await session.commit()
    # Age the reserve past the window.
    set_org_context(session, org_id)
    held.created_at = held.created_at - credits.STALE_RESERVE_AFTER - timedelta(minutes=1)
    await session.commit()
    released = await credits.release_stale_reserves(session, org_id)
    await session.commit()
    assert released == 1
    assert await credits.balance(session, org_id) == 2_000_000
    # Second sweep: nothing left to release.
    assert await credits.release_stale_reserves(session, org_id) == 0


def _org_obj(**kw) -> Org:
    o = Org(id=uuid.uuid4(), name="x", slug=f"x-{uuid.uuid4().hex[:6]}")
    o.ai_key_mode = kw.get("ai_key_mode", "platform")
    o.ai_markup_bps = kw.get("ai_markup_bps")
    o.ai_platform_fee_per_minute_micros = kw.get("fee")
    return o


def test_price_platform_mode_applies_default_then_override_markup():
    assert (
        credits.price_for(cost_micros=1_000_000, kind="llm", quantity=1, org=_org_obj())
        == 1_300_000
    )
    assert (
        credits.price_for(
            cost_micros=1_000_000, kind="llm", quantity=1, org=_org_obj(ai_markup_bps=5000)
        )
        == 1_500_000
    )
    # Rounds up, never gives a fraction of a micro away.
    assert credits.price_for(cost_micros=1, kind="llm", quantity=1, org=_org_obj()) == 2


def test_price_byok_voice_is_platform_fee_and_tokens_are_free():
    org = _org_obj(ai_key_mode="byok", fee=60_000)  # $0.06 / minute
    assert credits.price_for(cost_micros=999, kind="voice", quantity=90, org=org) == 90_000
    assert credits.price_for(cost_micros=999, kind="llm", quantity=5000, org=org) == 0
    assert credits.price_for(cost_micros=999, kind="tts", quantity=5000, org=org) == 0


def test_warning_levels():
    assert credits.warning_level(10_000_000, 10_000_000) is None
    assert credits.warning_level(2_000_000, 10_000_000) == "low"
    assert credits.warning_level(500_000, 10_000_000) == "critical"
    assert credits.warning_level(0, 10_000_000) == "empty"
    assert credits.warning_level(5, 0) is None
