"""Auto-pause on confirmed evidence only (operator decision 2026-09-27).

With automatic action off, the monitor may pause a workspace by itself ONLY when evidence the
platform confirmed (scam calls, held texts a review confirmed bad) reaches the pause score.
Anything else, and every `restricted`, stays a recommendation. The ops admins are emailed.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.db.base import set_org_context
from app.models import Org, PlatformOperator, SecurityAlert, User
from app.services import ai_guard, mailer, monitor_score
from tests.conftest import make_settings


@pytest.fixture
def detect_only():
    return make_settings(monitor_enforced=True, monitor_auto_action=False)


async def _org(session, name="Scam Co") -> uuid.UUID:
    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name=name, slug=f"auto-pause-{org_id.hex[:10]}"))
    await session.commit()
    return org_id


async def _admin(session, email: str) -> None:
    user = User(id=uuid.uuid4(), email=email, hashed_password="x")
    session.add(user)
    await session.flush()
    session.add(PlatformOperator(id=uuid.uuid4(), user_id=user.id, role="admin", is_active=True))
    await session.commit()


async def _signals(session, settings, org_id, kind: str, count: int):
    for n in range(count):
        await monitor_score.add_signal(session, settings, org_id, kind, f"{kind} {n}")
    await session.commit()
    set_org_context(session, org_id)
    return await monitor_score.get_state(session, org_id, create=True)


async def _ai_down(*_a, **_k):
    raise ai_guard.AIUnavailable("down")


async def test_confirmed_scam_calls_pause_and_email_the_admins(session, detect_only, monkeypatch):
    await _admin(session, "ops-admin@platform.example")
    org_id = await _org(session)
    state = await _signals(session, detect_only, org_id, "call_scam", 3)  # 3 x 40 = 120

    assert state.level == "paused"
    assert state.paused_at is not None
    assert monitor_score.recommended_level(state) is None
    assert state.case_file["status"] == "pending"
    alerts = (
        await session.execute(sa.select(SecurityAlert).where(SecurityAlert.org_id == org_id))
    ).scalars().all()
    assert "traffic_paused" in {a.kind for a in alerts}

    mailer.outbox.clear()
    monkeypatch.setattr(monitor_score.ai_guard, "judge", _ai_down)
    assert await monitor_score.case_file_tick(session, detect_only) >= 0
    sent = [m for m in mailer.outbox if "ops-admin@platform.example" in m["To"]]
    assert len(sent) == 1
    assert "paused automatically" in sent[0]["Subject"]

    # Next pass: no second email to the admins.
    mailer.outbox.clear()
    await monitor_score.case_file_tick(session, detect_only)
    assert not [m for m in mailer.outbox if "ops-admin@platform.example" in m["To"]]


async def test_unconfirmed_blocked_texts_still_only_recommend(session, detect_only):
    org_id = await _org(session)
    state = await _signals(session, detect_only, org_id, "text_blocked", 5)  # 125, unconfirmed

    assert state.score >= detect_only.monitor_pause_score
    assert state.level != "paused"
    assert monitor_score.recommended_level(state) == "paused"


async def test_confirmed_evidence_below_the_pause_score_only_recommends(session, detect_only):
    org_id = await _org(session)
    await _signals(session, detect_only, org_id, "call_scam", 1)  # 40 confirmed
    state = await _signals(session, detect_only, org_id, "text_blocked", 3)  # +75 = 115

    assert state.score >= detect_only.monitor_pause_score
    assert state.level != "paused"
    assert monitor_score.recommended_level(state) == "paused"


async def test_restricted_is_never_applied_automatically(session, detect_only):
    org_id = await _org(session)
    state = await _signals(session, detect_only, org_id, "call_scam", 2)  # 80: restricted band

    assert state.level != "restricted"
    assert monitor_score.recommended_level(state) == "restricted"


async def test_only_an_operator_ends_an_auto_pause(session, detect_only):
    org_id = await _org(session)
    state = await _signals(session, detect_only, org_id, "call_scam", 3)
    assert state.level == "paused"
    state.cleared_before = monitor_score._now()
    await monitor_score.recompute(session, detect_only, state)
    assert state.level == "paused"
