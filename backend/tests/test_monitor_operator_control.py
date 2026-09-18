"""The operator is the decision-maker: the AI detects, a human acts.

Four properties the operator asked for, each pinned here because each is a policy that a
future change could quietly reverse:
  1. Reaching a restricting score produces a RECOMMENDATION, not a restriction.
  2. An operator can order a thorough review of one account at any time.
  3. Every customer is visible with its own monitor, at 100-1000 accounts.
  4. Applying a recommendation is a human action, gated and audited; rejecting it clears it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import MonitorSignal, Org, OrgMonitoring
from app.services import monitor_score
from tests.conftest import make_settings

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def detect_only():
    """The shipped default: the monitor detects and never restricts."""
    return make_settings(monitor_enforced=True, monitor_auto_action=False)


@pytest.fixture
def auto_action():
    """The opt-in: automatic enforcement, as the system behaved before this policy."""
    return make_settings(monitor_enforced=True, monitor_auto_action=True)


async def _org(session, name="Acme Ltd") -> uuid.UUID:
    org_id = uuid.uuid4()
    slug = f"{name.lower().replace(chr(32), chr(45))}-{org_id.hex[:6]}"
    session.add(Org(id=org_id, name=name, slug=slug))
    await session.commit()
    set_org_context(session, org_id)
    return org_id


async def _pile_on_signals(session, settings, org_id, *, weight: int, count: int) -> OrgMonitoring:
    """Push the score past the pause threshold with platform-observed (hard) signals."""
    for n in range(count):
        await monitor_score.add_signal(
            session, settings, org_id, "text_blocked", f"blocked {n}", weight=weight
        )
    await session.commit()
    set_org_context(session, org_id)
    return await monitor_score.get_state(session, org_id, create=True)


# ======================================================================================
# 1. Detection without action
# ======================================================================================
async def test_a_pausing_score_only_recommends_when_auto_action_is_off(session, detect_only):
    org_id = await _org(session)
    state = await _pile_on_signals(session, detect_only, org_id, weight=40, count=3)

    assert state.score >= detect_only.monitor_pause_score
    # The account is NOT restricted. It reached `watch` on its own - scrutiny, which
    # throttles nothing - and then stopped and asked for a person. That is the whole policy.
    assert state.level == "watch"
    assert state.paused_at is None
    rec = monitor_score.recommended_level(state)
    assert rec == "paused", state.case_file
    assert (state.case_file or {}).get("recommendation", {}).get("score") == state.score


async def test_the_same_score_does_restrict_when_auto_action_is_on(session, auto_action):
    """Control: the policy is a switch, not a removal. With it on, the old behaviour is intact."""
    org_id = await _org(session)
    state = await _pile_on_signals(session, auto_action, org_id, weight=40, count=3)

    assert state.level == "paused"
    assert state.paused_at is not None
    assert monitor_score.recommended_level(state) is None


async def test_watch_still_escalates_on_its_own(session, detect_only):
    """`watch` restricts nothing - it only makes calls get reviewed - so the AI may raise its
    own scrutiny without a human. If this ever needed a decision too, every account would
    queue for review on its first weak signal and the load would be worse, not better."""
    org_id = await _org(session)
    state = await _pile_on_signals(session, detect_only, org_id, weight=15, count=2)

    assert detect_only.monitor_watch_score <= state.score < detect_only.monitor_restrict_score
    assert state.level == "watch"
    assert monitor_score.recommended_level(state) is None


async def test_a_pause_recommendation_still_needs_hard_evidence(session, detect_only):
    """The soft-signal rule survives: public reports alone must not even RECOMMEND a pause,
    or a competitor could fill an operator's queue with accounts to switch off."""
    org_id = await _org(session)
    for n in range(20):
        await monitor_score.add_signal(
            session, detect_only, org_id, "public_report", f"report {n}", weight=20
        )
    await session.commit()
    set_org_context(session, org_id)
    state = await monitor_score.get_state(session, org_id, create=True)

    assert state.score >= detect_only.monitor_pause_score
    assert monitor_score.recommended_level(state) == "restricted", state.case_file


# ======================================================================================
# 4. The human decision
# ======================================================================================
async def test_applying_a_recommendation_is_what_restricts_the_account(session, detect_only):
    org_id = await _org(session)
    state = await _pile_on_signals(session, detect_only, org_id, weight=40, count=3)
    assert state.level == "watch", "nothing restricting may happen without a human"

    # What the ops endpoint does when an operator clicks "apply".
    recommended = monitor_score.recommended_level(state)
    state.level = recommended
    monitor_score.clear_recommendation(state)
    await session.commit()

    assert state.level == "paused"
    assert monitor_score.recommended_level(state) is None


async def test_rejecting_a_recommendation_does_not_leave_it_to_reappear(session, detect_only):
    """A rejected recommendation must be spent. Without clearing the score, the very next
    recompute would recommend the same thing again and the operator's decision would be
    undone by the arithmetic."""
    org_id = await _org(session)
    state = await _pile_on_signals(session, detect_only, org_id, weight=40, count=3)
    assert monitor_score.recommended_level(state) == "paused"

    monitor_score.clear_recommendation(state)
    state.cleared_before = datetime.now(timezone.utc)
    state.score = 0
    await session.commit()

    await monitor_score.recompute(session, detect_only, state)
    await session.commit()
    assert monitor_score.recommended_level(state) is None, "the rejected recommendation returned"
    assert state.level == "normal"


# ======================================================================================
# 2 + 3. On demand, and per customer at scale
# ======================================================================================
async def test_review_account_reports_without_acting(session, detect_only):
    """The operator's button: it reads, it records, it does not act."""
    from app.services import monitor_review

    org_id = await _org(session, "Dan's Plumbing")
    state = await _pile_on_signals(session, detect_only, org_id, weight=40, count=3)
    assert state.level == "watch", "scrutiny only - no restriction without a human"

    report = await monitor_review.review_account(session, detect_only, org_id, days=7)

    assert report["account"]["org_id"] == str(org_id)
    assert report["actions_taken"] == []
    assert report["monitor"]["recommendation"]["level"] == "paused"
    assert report["monitor"]["level"] == "watch", "a review must not restrict the account"
    assert "headline" in report
    assert report["window_days"] == 7
    # It surfaces the evidence the operator would otherwise dig for.
    assert len(report["signals"]) == 3
    assert report["traffic"]["outbound_texts"] == 0


async def test_every_customer_has_its_own_monitor_state(session, detect_only):
    """At 100-1000 customers each account carries its own score, level and recommendation -
    one noisy account must not colour its neighbours."""
    ids = []
    for n in range(5):
        org_id = await _org(session, f"Customer {n}")
        ids.append(org_id)
        if n == 2:
            await _pile_on_signals(session, detect_only, org_id, weight=40, count=3)
        else:
            await monitor_score.add_signal(
                session, detect_only, org_id, "stop_rate", "one weak signal", weight=5
            )
    await session.commit()

    states = {}
    for org_id in ids:
        set_org_context(session, org_id)
        states[org_id] = await monitor_score.get_state(session, org_id, create=True)

    flagged = [i for i, s in states.items() if monitor_score.recommended_level(s)]
    assert flagged == [ids[2]], "exactly one account should need a decision"
    assert states[ids[2]].level == "watch", "the flagged account raised scrutiny only"
    assert all(states[i].level == "normal" for i in ids if i != ids[2])
    assert states[ids[2]].score > states[ids[0]].score

    # And the signals never leak across accounts.
    for org_id in ids:
        set_org_context(session, org_id)
        rows = (
            await session.execute(
                sa.select(sa.func.count(MonitorSignal.id)).where(MonitorSignal.org_id == org_id)
            )
        ).scalar_one()
        assert rows == (3 if org_id == ids[2] else 1)
