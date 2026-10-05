"""AI minute billing: the flat per-started-minute price, the plan's ``ai_minutes``
allowance, and the all-in components that ride along inside it.

The seam these tests defend is that an AI minute is priced ONCE - ``ceil(seconds/60)``
started minutes at ``ai_minute_price_micros``, less whatever the plan's ``ai_minutes``
allowance still covers - while the speech/voice/model metrics that make up a call stay in
the P&L as ``cost_micros`` and are never billed on their own. Enforce mode spends the
allowance and debits prepaid credits; shadow mode only peeks, so the customer sees the
number they would have paid and nobody is charged.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import AiUsageEvent, Call, CreditLedgerEntry, Org
from app.models.plans import Plan
from app.services import ai_usage, credits, entitlements, plan_billing
from app.services import plans as plans_svc
from tests.conftest import make_settings

#: The default flat price of one started AI minute, in micros.
AI_MINUTE_MICROS = 350_000


# ----------------------------------------------------------------------------------
# Helpers (copied from tests/test_p24_usage.py and tests/test_p37c_plans.py - private
# test helpers are not imported across test files)
# ----------------------------------------------------------------------------------
async def _make_org(session, name="AI Minute Org", *, plan_code=None):
    org = Org(
        id=uuid.uuid4(),
        name=name,
        slug=f"ai-min-{uuid.uuid4().hex[:16]}",
        plan_code=plan_code,
        plan_started_at=datetime.now(timezone.utc) if plan_code else None,
    )
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


async def _seed_catalog(session, settings):
    """Write the real plan catalogue, so an org with a ``plan_code`` has a row to point at
    (``orgs.plan_code`` is a foreign key to ``plans.code``) and the plan grants its allowance."""
    await plan_billing.ensure_catalog(session, settings)
    await session.commit()


async def _record_ai_minutes(session, org_id, seconds, key, *, settings=None):
    return await ai_usage.record(
        session,
        org_id,
        provider="livekit",
        kind="voice",
        metric=ai_usage.AI_MINUTE_METRIC,
        quantity=seconds,
        source="worker",
        idempotency_key=key,
        settings=settings,
    )


async def _event_count(session, org_id) -> int:
    set_org_context(session, org_id)
    return int(
        (await session.execute(sa.select(sa.func.count(AiUsageEvent.id)))).scalar_one()
    )


async def _usage_charge_count(session, org_id) -> int:
    """How many prepaid-credit debits (entry_type ``usage``) this org carries."""
    set_org_context(session, org_id)
    return int(
        (
            await session.execute(
                sa.select(sa.func.count(CreditLedgerEntry.id)).where(
                    CreditLedgerEntry.entry_type == "usage"
                )
            )
        ).scalar_one()
    )


# ==================================================================================
# 1-2. No plan: the flat price is the whole bill
# ==================================================================================
async def test_shadow_mode_with_no_plan_prices_two_started_minutes_and_never_debits(session):
    org = await _make_org(session)
    await credits.topup(session, org.id, 10_000_000, reference="pi-ai-shadow")
    await session.commit()

    before = await credits.balance(session, org.id)
    event = await _record_ai_minutes(session, org.id, 61, "ai-shadow-no-plan")
    await session.commit()

    # 61 s is two STARTED minutes, each at the default 350_000 micros.
    assert event.price_micros == 700_000
    # Shadow mode shows the price but moves no money.
    assert await credits.balance(session, org.id) == before
    assert await _usage_charge_count(session, org.id) == 0
    assert await _event_count(session, org.id) == 1


async def test_enforce_mode_with_no_plan_debits_the_flat_price_exactly_once(session):
    enforce = make_settings(ai_billing_enforce=True)
    org = await _make_org(session)
    await credits.topup(session, org.id, 10_000_000, reference="pi-ai-enforce")
    await session.commit()

    before = await credits.balance(session, org.id)
    first = await _record_ai_minutes(
        session, org.id, 61, "ai-enforce-no-plan", settings=enforce
    )
    await session.commit()

    assert first.price_micros == 700_000
    assert await credits.balance(session, org.id) == before - 700_000
    assert await _usage_charge_count(session, org.id) == 1

    after_first = await credits.balance(session, org.id)
    second = await _record_ai_minutes(
        session, org.id, 61, "ai-enforce-no-plan", settings=enforce
    )
    await session.commit()

    # A replayed idempotency key returns the same event and charges nothing more.
    assert second.id == first.id
    assert await credits.balance(session, org.id) == after_first
    assert await _usage_charge_count(session, org.id) == 1
    assert await _event_count(session, org.id) == 1


# ==================================================================================
# 3-4. The plan allowance is spent first, and only in enforce mode
# ==================================================================================
async def test_enforce_mode_spends_the_team_allowance_then_bills_ten_minutes_of_overage(session):
    enforce = make_settings(ai_billing_enforce=True)
    await _seed_catalog(session, enforce)
    org = await _make_org(session, plan_code="team")
    await credits.topup(session, org.id, 10_000_000, reference="pi-ai-team")
    await session.commit()

    first = await _record_ai_minutes(session, org.id, 1200, "ai-team-1", settings=enforce)
    second = await _record_ai_minutes(session, org.id, 1200, "ai-team-2", settings=enforce)
    third = await _record_ai_minutes(session, org.id, 1200, "ai-team-3", settings=enforce)
    fourth = await _record_ai_minutes(session, org.id, 60, "ai-team-4", settings=enforce)
    await session.commit()

    assert first.price_micros == 0
    assert second.price_micros == 0
    # 50 included minutes: the third 20-minute call has only 10 minutes of allowance left.
    assert third.price_micros == 10 * AI_MINUTE_MICROS
    # Allowance exhausted, so the fourth started minute is billed flat.
    assert fourth.price_micros == AI_MINUTE_MICROS
    assert await plans_svc.remaining(session, org.id, "ai_minutes") == 0

    assert await credits.balance(session, org.id) == (
        10_000_000 - 10 * AI_MINUTE_MICROS - AI_MINUTE_MICROS
    )
    assert await _usage_charge_count(session, org.id) == 2


async def test_shadow_team_plan_shows_covered_price_and_keeps_allowance(session):
    shadow = make_settings(ai_billing_enforce=False)
    await _seed_catalog(session, shadow)
    org = await _make_org(session, plan_code="team")
    await session.commit()

    event = await _record_ai_minutes(session, org.id, 1200, "ai-team-shadow", settings=shadow)
    await session.commit()

    assert event.price_micros == 0
    # The peek must not have spent anything: the whole 50-minute allowance is still there.
    assert await plans_svc.remaining(session, org.id, "ai_minutes") == 50
    assert await _usage_charge_count(session, org.id) == 0


# ==================================================================================
# 5-6. Components: inside the all-in AI minute on a call, cost+markup off it
# ==================================================================================
async def test_stt_on_a_call_is_metered_but_inside_the_all_in_ai_minute(session):
    org = await _make_org(session)
    call = await _make_call(session, org.id)

    event = await ai_usage.record(
        session,
        org.id,
        provider="deepgram",
        kind="stt",
        metric="stt_seconds",
        quantity=60,
        source="worker",
        idempotency_key="all-in-stt",
        call_id=call.id,
    )
    await session.commit()

    # Known rate (deepgram stt_seconds = 100 micros/s): the cost is still in the P&L...
    assert event.cost_micros == 6_000
    # ...but on a call it is inside the all-in AI minute, so it is never billed on its own.
    assert event.price_micros == 0


async def test_llm_tokens_out_without_a_call_is_still_billed_cost_plus_markup(session):
    # NOTE: this is the SMS/thread path. Attaching a real MessageThread would need the thread
    # model's required columns, which are not part of the context for this file - and the
    # pricing branch keys off the ABSENCE of a call_id alone, so the thread_id is left off
    # rather than guessed at.
    org = await _make_org(session)

    event = await ai_usage.record(
        session,
        org.id,
        provider="openai",
        kind="llm",
        metric="llm_tokens_out",
        quantity=100,
        source="worker",
        idempotency_key="no-call-tokens-out",
    )
    await session.commit()

    # 100 tokens x 10 micros (openai llm_tokens_out).
    assert event.cost_micros == 1_000
    # No call, so the all-in rule does not apply and the event keeps its cost+markup price.
    assert event.price_micros > 0


# ==================================================================================
# 7. reserve sizing
# ==================================================================================
async def test_estimate_call_price_is_the_flat_ai_minute_rate_for_a_platform_workspace(session):
    org = await _make_org(session)
    settings = make_settings()

    assert (
        await ai_usage.estimate_call_price(
            session, org, SimpleNamespace(max_call_seconds=900), settings=settings
        )
        == 15 * AI_MINUTE_MICROS
    )
    assert (
        await ai_usage.estimate_call_price(
            session, org, SimpleNamespace(max_call_seconds=61), settings=settings
        )
        == 2 * AI_MINUTE_MICROS
    )


# ==================================================================================
# 8. Catalogue
# ==================================================================================
async def test_the_catalogue_sells_ai_minutes_as_a_plan_allowance(session):
    assert plan_billing.PLANS["solo"].ai_minutes == 0
    assert plan_billing.PLANS["team"].ai_minutes == 50
    assert plan_billing.PLANS["business"].ai_minutes == 200
    assert "ai_minutes" in plans_svc.ALLOWANCE_METRICS

    await plan_billing.ensure_catalog(session, make_settings())
    await session.commit()

    for code, minutes in (("solo", 0), ("team", 50), ("business", 200)):
        plan = await session.get(Plan, code)
        assert plan is not None
        assert plan.included["ai_minutes"] == minutes


# ==================================================================================
# 9. The plan lock and its explicit override
# ==================================================================================
async def test_a_solo_plan_locks_the_ai_agent_until_an_explicit_override(session):
    # The catalogue must exist first: orgs.plan_code is a foreign key to plans.code.
    await _seed_catalog(session, make_settings())
    solo = await _make_org(session, plan_code="solo")
    team = await _make_org(session, plan_code="team")
    planless = await _make_org(session)

    assert (await entitlements.for_org(session, solo.id))["ai_agent"] is False
    assert (await entitlements.for_org(session, team.id))["ai_agent"] is True
    assert (await entitlements.for_org(session, planless.id))["ai_agent"] is True

    await entitlements.set_feature(
        session,
        solo.id,
        "ai_agent",
        enabled=True,
        price_override_micros=None,
        actor_user_id=None,
    )
    await session.commit()

    # An explicit OrgFeature row beats the plan's default lock.
    assert (await entitlements.for_org(session, solo.id))["ai_agent"] is True


# ==================================================================================
# 10. BYOK is unchanged
# ==================================================================================
async def test_a_byok_workspace_pays_the_platform_fee_not_the_flat_ai_minute(session):
    org = await _make_org(session)
    org.ai_key_mode = "byok"
    org.ai_platform_fee_per_minute_micros = 60_000
    await session.commit()

    event = await _record_ai_minutes(
        session, org.id, 120, "ai-byok-voice", settings=make_settings()
    )
    await session.commit()

    # BYOK voice is unchanged: two started minutes at the workspace's own platform fee of
    # 60_000 micros - the flat 350_000 AI minute never applies.
    assert event.price_micros == 120_000
