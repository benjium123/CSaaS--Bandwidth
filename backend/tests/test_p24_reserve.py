from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import (
    AgentProfile,
    AuditLogEntry,
    Call,
    ContactList,
    CreditLedgerEntry,
    Org,
    OutboundCampaign,
    PlatformEvent,
)
from app.services import ai_usage, credits
from tests.conftest import make_settings


async def _make_org(session, name="Reserve Org"):
    org = Org(id=uuid.uuid4(), name=name, slug=f"reserve-{uuid.uuid4().hex[:16]}")
    session.add(org)
    await session.commit()
    await session.refresh(org)
    return org


async def _make_profile(
    session,
    org_id,
    *,
    name="Agent",
    max_call_seconds=100,
):
    set_org_context(session, org_id)
    profile = AgentProfile(
        id=uuid.uuid4(),
        org_id=org_id,
        name=name,
        system_prompt="",
        greeting="",
        voice_id="",
        llm_provider="",
        llm_model="",
        voicemail_message="",
        is_default=False,
        goals="",
        guardrails="",
        language="en",
        stt_provider="",
        tts_provider="",
        max_call_seconds=max_call_seconds,
        silence_timeout_seconds=12,
        interrupt_sensitivity="medium",
    )
    session.add(profile)
    await session.commit()
    await session.refresh(profile)
    return profile


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


async def _make_contact_list(session, org_id):
    set_org_context(session, org_id)
    contact_list = ContactList(
        id=uuid.uuid4(),
        org_id=org_id,
        name="list",
    )
    session.add(contact_list)
    await session.commit()
    return contact_list


async def _make_campaign(session, org_id, list_id, *, status="running"):
    set_org_context(session, org_id)
    campaign = OutboundCampaign(
        id=uuid.uuid4(),
        org_id=org_id,
        name="campaign",
        channel="sms",
        list_id=list_id,
        status=status,
    )
    session.add(campaign)
    await session.commit()
    return campaign


async def _warning_events(session, org_id, level=None):
    set_org_context(session, org_id)
    rows = (
        await session.execute(
            sa.select(PlatformEvent).where(
                PlatformEvent.event_type == "billing.low_balance"
            )
        )
    ).scalars().all()

    result = []
    for event in rows:
        payload = event.payload or {}
        if payload.get("kind") == "warning":
            result.append(
                {
                    "level": payload.get("level"),
                    "dedupe_key": payload.get("dedupe_key"),
                }
            )

    if level is not None:
        return [row for row in result if row["level"] == level]
    return result


async def test_reserve_holds_credits_and_release_gives_them_back(session):
    org = await _make_org(session)
    profile = await _make_profile(session, org.id, max_call_seconds=100)
    call = await _make_call(session, org.id)
    await credits.topup(session, org.id, 10_000_000, reference="pi-reserve-1")
    await session.commit()

    before = await credits.balance(session, org.id)
    enforce = make_settings(ai_billing_enforce=True)

    result = await ai_usage.reserve_for_call(
        session, org, profile, call, settings=enforce
    )
    assert result["ok"] is True
    estimate = result["reserved_micros"]
    assert estimate > 0
    await session.commit()

    after_hold = await credits.balance(session, org.id)
    assert after_hold == before - estimate
    assert await credits.outstanding_reserves(session, org.id) == estimate

    released = await ai_usage.release_for_call(session, org.id, call.id)
    assert released is True
    await session.commit()

    assert await credits.balance(session, org.id) == before
    assert await credits.outstanding_reserves(session, org.id) == 0

    released_again = await ai_usage.release_for_call(session, org.id, call.id)
    assert released_again is False
    await session.commit()

    assert await credits.balance(session, org.id) == before
    assert await credits.outstanding_reserves(session, org.id) == 0


async def test_reserve_is_refused_in_plain_words_when_credits_run_out(session):
    org = await _make_org(session)
    profile = await _make_profile(session, org.id, max_call_seconds=100)
    call = await _make_call(session, org.id)
    await credits.topup(session, org.id, 1, reference="tiny-reserve")
    await session.commit()

    enforce = make_settings(ai_billing_enforce=True)
    result = await ai_usage.reserve_for_call(
        session, org, profile, call, settings=enforce
    )

    assert result["ok"] is False
    assert result["message"] == "Add credits to keep your assistant answering."
    for forbidden in ("micros", "ledger", "reserve", "token"):
        assert forbidden not in result["message"]


async def test_refused_reserve_reports_the_workspace_fallback(session):
    org = await _make_org(session)
    profile = await _make_profile(session, org.id)
    call = await _make_call(session, org.id)
    await credits.topup(session, org.id, 1, reference="tiny-fallback-1")
    org.credit_auto_recharge = {"fallback": "human_flow"}
    await session.commit()
    await session.refresh(org)

    enforce = make_settings(ai_billing_enforce=True)
    result = await ai_usage.reserve_for_call(
        session, org, profile, call, settings=enforce
    )

    assert result["ok"] is False
    assert result["fallback"] == "human_flow"

    org2 = await _make_org(session, "No Auto Recharge")
    profile2 = await _make_profile(session, org2.id)
    call2 = await _make_call(session, org2.id)
    await credits.topup(session, org2.id, 1, reference="tiny-fallback-2")
    await session.commit()

    result2 = await ai_usage.reserve_for_call(
        session, org2, profile2, call2, settings=enforce
    )

    assert result2["ok"] is False
    assert result2["fallback"] == "busy"


async def test_shadow_mode_never_refuses_a_call(session):
    org = await _make_org(session)
    profile = await _make_profile(session, org.id)
    call = await _make_call(session, org.id)

    result = await ai_usage.reserve_for_call(
        session, org, profile, call, settings=make_settings()
    )

    assert result["ok"] is True
    assert result["reserved_micros"] == 0

    set_org_context(session, org.id)
    ledger_count = (
        await session.execute(sa.select(sa.func.count(CreditLedgerEntry.id)))
    ).scalar_one()
    assert ledger_count == 0


async def test_a_repeated_reserve_for_the_same_call_holds_once(session):
    org = await _make_org(session)
    profile = await _make_profile(session, org.id, max_call_seconds=100)
    call = await _make_call(session, org.id)
    await credits.topup(session, org.id, 10_000_000, reference="pi-repeat")
    await session.commit()

    enforce = make_settings(ai_billing_enforce=True)
    first = await ai_usage.reserve_for_call(
        session, org, profile, call, settings=enforce
    )
    after_first = await credits.balance(session, org.id)

    second = await ai_usage.reserve_for_call(
        session, org, profile, call, settings=enforce
    )
    after_second = await credits.balance(session, org.id)

    assert first["ok"] is True
    assert second["ok"] is True
    assert after_second == after_first
    await session.commit()

    set_org_context(session, org.id)
    reserve_rows = (
        await session.execute(
            sa.select(CreditLedgerEntry).where(
                CreditLedgerEntry.entry_type == "reserve"
            )
        )
    ).scalars().all()
    assert len(reserve_rows) == 1
    assert await credits.outstanding_reserves(session, org.id) == first["reserved_micros"]


async def test_the_sweeper_releases_a_stale_reserve(session):
    org = await _make_org(session)
    profile = await _make_profile(session, org.id, max_call_seconds=100)
    call = await _make_call(session, org.id)
    await credits.topup(session, org.id, 10_000_000, reference="pi-stale")
    await session.commit()

    enforce = make_settings(ai_billing_enforce=True)
    await ai_usage.reserve_for_call(session, org, profile, call, settings=enforce)
    await session.commit()

    stale_time = datetime.now(timezone.utc) - timedelta(hours=1)
    await session.execute(
        sa.update(CreditLedgerEntry)
        .where(
            CreditLedgerEntry.org_id == org.id,
            CreditLedgerEntry.entry_type == "reserve",
            CreditLedgerEntry.reference == ai_usage.call_reserve_reference(call.id),
        )
        .values(created_at=stale_time)
    )
    await session.commit()

    totals = await ai_usage.credits_tick(session, settings=make_settings())

    assert totals["reserves_released"] == 1
    assert await credits.balance(session, org.id) == 10_000_000
    assert await credits.outstanding_reserves(session, org.id) == 0


async def test_a_low_balance_warning_fires_once_per_level_per_topup_cycle(session):
    org = await _make_org(session)
    await credits.topup(session, org.id, 10_000_000, reference="pi_a")
    await session.commit()

    await credits.charge_usage(session, org.id, 8_500_000, reference="spend-low")
    await session.commit()

    first_low = await ai_usage.check_balance_warnings(session, org)
    assert first_low is not None
    assert first_low["level"] == "low"
    await session.commit()

    assert await ai_usage.check_balance_warnings(session, org) is None
    await session.commit()

    low_events = await _warning_events(session, org.id, level="low")
    assert len(low_events) == 1

    await credits.charge_usage(session, org.id, 1_200_000, reference="spend-critical")
    await session.commit()

    critical = await ai_usage.check_balance_warnings(session, org)
    assert critical is not None
    assert critical["level"] == "critical"
    await session.commit()

    critical_events = await _warning_events(session, org.id, level="critical")
    assert len(critical_events) == 1

    await credits.topup(session, org.id, 1_000_000, reference="pi_b")
    await session.commit()
    await credits.charge_usage(session, org.id, 1_150_000, reference="spend-low-b")
    await session.commit()

    low_again = await ai_usage.check_balance_warnings(session, org)
    assert low_again is not None
    assert low_again["level"] == "low"
    await session.commit()

    low_events = await _warning_events(session, org.id, level="low")
    assert len(low_events) == 2


async def test_an_empty_balance_pauses_the_running_campaigns(session):
    org = await _make_org(session)
    contact_list = await _make_contact_list(session, org.id)
    campaign = await _make_campaign(session, org.id, contact_list.id, status="running")

    await credits.topup(session, org.id, 10_000_000, reference="pi-empty")
    await session.commit()
    await credits.charge_usage(session, org.id, 10_000_000, reference="spend-empty")
    await session.commit()

    totals = await ai_usage.credits_tick(session, settings=make_settings())

    assert totals["campaigns_paused"] == 1

    set_org_context(session, org.id)
    await session.refresh(campaign)
    assert campaign.status == "paused"

    audit_count = (
        await session.execute(
            sa.select(sa.func.count(AuditLogEntry.id)).where(
                AuditLogEntry.action == "campaign.paused"
            )
        )
    ).scalar_one()
    assert audit_count == 1


async def test_the_sweeper_pass_is_scoped_per_workspace(session):
    empty_org = await _make_org(session, "Empty Workspace")
    rich_org = await _make_org(session, "Rich Workspace")

    empty_list = await _make_contact_list(session, empty_org.id)
    rich_list = await _make_contact_list(session, rich_org.id)
    empty_campaign = await _make_campaign(
        session, empty_org.id, empty_list.id, status="running"
    )
    rich_campaign = await _make_campaign(
        session, rich_org.id, rich_list.id, status="running"
    )

    await credits.topup(session, empty_org.id, 10_000_000, reference="pi-empty-2")
    await session.commit()
    await credits.charge_usage(
        session, empty_org.id, 10_000_000, reference="spend-empty-2"
    )
    await session.commit()

    await credits.topup(session, rich_org.id, 10_000_000, reference="pi-rich")
    await session.commit()

    totals = await ai_usage.credits_tick(session, settings=make_settings())

    assert totals["campaigns_paused"] == 1

    set_org_context(session, empty_org.id)
    await session.refresh(empty_campaign)
    assert empty_campaign.status == "paused"

    set_org_context(session, rich_org.id)
    await session.refresh(rich_campaign)
    assert rich_campaign.status == "running"


async def test_estimate_never_returns_a_negative_price(session):
    org = await _make_org(session)
    profile = await _make_profile(session, org.id, max_call_seconds=100)

    estimate = await ai_usage.estimate_call_price(session, org, profile)

    assert estimate >= 0


async def test_estimate_uses_the_profile_max_call_seconds(session):
    org = await _make_org(session)
    profile_600 = await _make_profile(
        session, org.id, name="p600", max_call_seconds=600
    )
    profile_900 = await _make_profile(
        session, org.id, name="p900", max_call_seconds=900
    )

    estimate_600 = await ai_usage.estimate_call_price(session, org, profile_600)
    estimate_900 = await ai_usage.estimate_call_price(session, org, profile_900)

    assert estimate_600 > 0
    assert estimate_600 < estimate_900