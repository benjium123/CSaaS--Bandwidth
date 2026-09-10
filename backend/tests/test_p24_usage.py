from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.models import AiUsageEvent, Call, CreditLedgerEntry, Org
from app.services import ai_usage, credits
from tests.conftest import make_settings


async def _make_org(session, name="Usage Org"):
    org = Org(id=uuid.uuid4(), name=name, slug=f"usage-{uuid.uuid4().hex[:16]}")
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return org


async def _make_call(session, org_id):
    set_org_context(session, org_id)
    call = Call(
        id=uuid.uuid4(),
        org_id=org_id,
        direction="outbound",
        contact_e164="+19725550199",
        our_e164="+12145550100",
        carrier="test",
    )
    session.add(call)
    await session.commit()
    return call


async def test_usage_event_is_written_once_per_idempotency_key(session):
    org = await _make_org(session)
    key = f"evt-{uuid.uuid4()}"

    first = await ai_usage.record(
        session,
        org.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_in",
        quantity=10,
        source="worker",
        idempotency_key=key,
    )
    second = await ai_usage.record(
        session,
        org.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_in",
        quantity=10,
        source="worker",
        idempotency_key=key,
    )

    assert first.id == second.id

    set_org_context(session, org.id)
    count = (
        await session.execute(sa.select(sa.func.count(AiUsageEvent.id)))
    ).scalar_one()
    assert count == 1


async def test_shadow_mode_writes_the_event_and_never_debits(session):
    org = await _make_org(session)
    await credits.topup(session, org.id, 10_000_000, reference="pi-shadow")
    await session.commit()

    before = await credits.balance(session, org.id)
    await ai_usage.record(
        session,
        org.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_in",
        quantity=10,
        source="worker",
        idempotency_key="evt-shadow",
    )
    await session.commit()

    after = await credits.balance(session, org.id)
    assert after == before

    set_org_context(session, org.id)
    usage_count = (
        await session.execute(
            sa.select(sa.func.count(CreditLedgerEntry.id)).where(
                CreditLedgerEntry.entry_type == "usage"
            )
        )
    ).scalar_one()
    assert usage_count == 0

    event_count = (
        await session.execute(sa.select(sa.func.count(AiUsageEvent.id)))
    ).scalar_one()
    assert event_count == 1


async def test_enforce_mode_debits_the_customer_price(session):
    enforce = make_settings(ai_billing_enforce=True)
    org = await _make_org(session)
    await credits.topup(session, org.id, 10_000_000, reference="pi-enforce")
    await session.commit()

    before = await credits.balance(session, org.id)
    event = await ai_usage.record(
        session,
        org.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_in",
        quantity=100,
        source="worker",
        idempotency_key="evt-enforce",
        settings=enforce,
    )
    await session.commit()

    after = await credits.balance(session, org.id)
    assert after == before - event.price_micros

    set_org_context(session, org.id)
    usage_row = (
        await session.execute(
            sa.select(CreditLedgerEntry).where(
                CreditLedgerEntry.entry_type == "usage",
                CreditLedgerEntry.reference == f"usage:{event.id}",
            )
        )
    ).scalar_one_or_none()
    assert usage_row is not None
    assert usage_row.amount_micros == -event.price_micros


async def test_replayed_event_in_enforce_mode_does_not_debit_twice(session):
    enforce = make_settings(ai_billing_enforce=True)
    org = await _make_org(session)
    await credits.topup(session, org.id, 10_000_000, reference="pi-replay")
    await session.commit()

    key = "evt-replay"
    first = await ai_usage.record(
        session,
        org.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_in",
        quantity=100,
        source="worker",
        idempotency_key=key,
        settings=enforce,
    )
    after_first = await credits.balance(session, org.id)

    second = await ai_usage.record(
        session,
        org.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_in",
        quantity=100,
        source="worker",
        idempotency_key=key,
        settings=enforce,
    )
    after_second = await credits.balance(session, org.id)

    assert first.id == second.id
    assert after_second == after_first

    set_org_context(session, org.id)
    usage_count = (
        await session.execute(
            sa.select(sa.func.count(CreditLedgerEntry.id)).where(
                CreditLedgerEntry.entry_type == "usage",
                CreditLedgerEntry.reference == f"usage:{first.id}",
            )
        )
    ).scalar_one()
    assert usage_count == 1


async def test_price_is_cost_plus_the_platform_markup_then_the_org_override(session):
    org = await _make_org(session)

    first = await ai_usage.record(
        session,
        org.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_in",
        quantity=10,
        source="worker",
        idempotency_key="price-default",
    )
    expected_default = (first.cost_micros * 13_000 + 9_999) // 10_000
    assert first.price_micros == expected_default

    org.ai_markup_bps = 5000
    await session.commit()

    second = await ai_usage.record(
        session,
        org.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_in",
        quantity=10,
        source="worker",
        idempotency_key="price-override",
    )
    expected_override = (second.cost_micros * 15_000 + 9_999) // 10_000
    assert second.price_micros == expected_override


async def test_byok_voice_is_charged_the_platform_fee_and_byok_tokens_are_free(session):
    org = await _make_org(session)
    org.ai_key_mode = "byok"
    org.ai_platform_fee_per_minute_micros = 600_000
    await session.commit()

    voice = await ai_usage.record(
        session,
        org.id,
        provider="livekit",
        kind="voice",
        metric="ai_voice_seconds",
        quantity=60,
        source="worker",
        idempotency_key="byok-voice",
    )
    tokens = await ai_usage.record(
        session,
        org.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_in",
        quantity=100,
        source="worker",
        idempotency_key="byok-tokens",
    )

    assert voice.price_micros == 600_000
    assert tokens.price_micros == 0

    set_org_context(session, org.id)
    count = (
        await session.execute(sa.select(sa.func.count(AiUsageEvent.id)))
    ).scalar_one()
    assert count == 2


async def test_usage_event_refuses_a_call_from_another_workspace(session):
    org_a = await _make_org(session, "Call Owner")
    org_b = await _make_org(session, "Other Workspace")
    call_a = await _make_call(session, org_a.id)

    with pytest.raises(ValidationFailedError) as exc:
        await ai_usage.record(
            session,
            org_b.id,
            provider="openai",
            kind="llm",
            metric="llm_tokens_in",
            quantity=10,
            source="worker",
            idempotency_key="cross-org-call",
            call_id=call_a.id,
        )

    assert "call" in str(exc.value).lower()

    set_org_context(session, org_a.id)
    count_a = (
        await session.execute(sa.select(sa.func.count(AiUsageEvent.id)))
    ).scalar_one()

    set_org_context(session, org_b.id)
    count_b = (
        await session.execute(sa.select(sa.func.count(AiUsageEvent.id)))
    ).scalar_one()

    assert count_a == 0
    assert count_b == 0


async def test_usage_summary_and_call_drilldown_never_expose_cost(session):
    org = await _make_org(session)
    call = await _make_call(session, org.id)
    occurred = datetime.now(timezone.utc)

    await ai_usage.record(
        session,
        org.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_in",
        quantity=10,
        source="worker",
        idempotency_key="summary-1",
        call_id=call.id,
        occurred_at=occurred,
    )
    await ai_usage.record(
        session,
        org.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_out",
        quantity=4,
        source="worker",
        idempotency_key="summary-2",
        call_id=call.id,
        occurred_at=occurred,
    )
    await session.commit()

    start = occurred - timedelta(seconds=1)
    end = occurred + timedelta(seconds=1)
    summary = await ai_usage.usage_summary(session, org.id, start=start, end=end)

    assert len(summary) == 2
    for row in summary:
        assert set(row.keys()) == {"metric", "quantity", "price_micros"}

    drill = await ai_usage.usage_for_call(session, org.id, call.id)
    for event in drill["events"]:
        assert "cost_micros" not in event


async def test_usage_summary_is_scoped_to_one_workspace(session):
    org_a = await _make_org(session, "Summary A")
    org_b = await _make_org(session, "Summary B")
    occurred = datetime.now(timezone.utc)

    await ai_usage.record(
        session,
        org_a.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_in",
        quantity=10,
        source="worker",
        idempotency_key="summary-a",
        occurred_at=occurred,
    )
    await ai_usage.record(
        session,
        org_b.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_in",
        quantity=5,
        source="worker",
        idempotency_key="summary-b",
        occurred_at=occurred,
    )
    await session.commit()

    start = occurred - timedelta(seconds=1)
    end = occurred + timedelta(seconds=1)
    summary_a = await ai_usage.usage_summary(session, org_a.id, start=start, end=end)
    summary_b = await ai_usage.usage_summary(session, org_b.id, start=start, end=end)

    a_by_metric = {row["metric"]: row["quantity"] for row in summary_a}
    b_by_metric = {row["metric"]: row["quantity"] for row in summary_b}

    assert a_by_metric.get("llm_tokens_in") == 10
    assert b_by_metric.get("llm_tokens_in") == 5


async def test_an_unpriced_provider_still_meters_at_zero_cost(session):
    org = await _make_org(session)
    event = await ai_usage.record(
        session,
        org.id,
        provider="not-a-real-provider",
        kind="llm",
        metric="llm_tokens_in",
        quantity=10,
        source="worker",
        idempotency_key="unpriced",
    )

    assert event.cost_micros == 0
    assert event.price_micros == 0

    await session.commit()
    set_org_context(session, org.id)
    count = (
        await session.execute(sa.select(sa.func.count(AiUsageEvent.id)))
    ).scalar_one()
    assert count == 1
