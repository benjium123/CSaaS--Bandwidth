"""Adversarial audit (session voip-62): the telephony gate when the two enforcement
flags disagree.

KYC_ENFORCED and MONITOR_ENFORCED are separate switches on the same gate
(services/telephony_access.refusal). MONITOR_ENFORCED off is a deliberate rollout kill
switch. What is NOT deliberate is that an OPERATOR-applied pause is still writable while
the gate that would honour it is switched off -- and a comment in the operator route
asserts the opposite.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import make_settings


@pytest.fixture
def monitor_off_settings():
    """The rollout configuration: business verification on, traffic monitor switched off."""
    return make_settings(kyc_enforced=True, monitor_enforced=False)


@pytest.fixture
def monitor_on_settings():
    return make_settings(kyc_enforced=True, monitor_enforced=True)


async def _approved_but_paused_org(session, settings) -> uuid.UUID:
    """An APPROVED business (so business verification clears it to send) that the traffic
    monitor has since paused. This is the state that matters: the monitor's pause is the
    only thing standing between this org and the carrier."""
    from app.db.base import set_org_context
    from app.models import KycProfile, Org
    from app.services import monitor_score

    org_id = uuid.uuid4()
    session.add(Org(id=org_id, name="Paused Co", slug=f"paused-{org_id.hex[:8]}"))
    await session.commit()

    set_org_context(session, org_id)
    session.add(KycProfile(id=uuid.uuid4(), org_id=org_id, status="approved"))
    state = await monitor_score.get_state(session, org_id, create=True)
    state.level = "paused"
    state.paused_reason = "Risk score reached the pause threshold"
    await session.commit()
    return org_id


# ======================================================================================
# monitor_score.refusal (monitor_score.py:198) returns None IMMEDIATELY when
# MONITOR_ENFORCED is off, before it ever reads state.level. For an APPROVED business,
# the monitor's pause is the only thing in the gate that would stop it -- business
# verification has already cleared it (telephony_access.py:86-87).
#
# So turning MONITOR_ENFORCED off does not only stop NEW pauses: it silently RELEASES
# every account the monitor has already paused. OrgMonitoring.level stays "paused", the
# operator console and the customer's own pause banner keep saying paused, and traffic
# flows.
#
# Failure scenario: the monitor pauses a scam account. Someone switches MONITOR_ENFORCED
# off -- a rollout rollback, a bad env in one worker, a copied staging config. Every
# previously-paused scammer resumes texting and calling, and no screen anywhere says so.
# ======================================================================================
async def test_operator_pause_actually_blocks_texting(session, monitor_off_settings):
    from app.services import telephony_access

    org_id = await _approved_but_paused_org(session, monitor_off_settings)

    refusal = await telephony_access.refusal(session, monitor_off_settings, org_id, "sms")
    assert refusal == "account_paused", (
        "an org the operator console shows as PAUSED was cleared to send texts: "
        f"refusal() returned {refusal!r} because MONITOR_ENFORCED is off"
    )


async def test_operator_pause_actually_blocks_calling(session, monitor_off_settings):
    from app.services import telephony_access

    org_id = await _approved_but_paused_org(session, monitor_off_settings)

    refusal = await telephony_access.refusal(session, monitor_off_settings, org_id, "call")
    assert refusal == "account_paused", (
        "an org the operator console shows as PAUSED was cleared to place calls: "
        f"refusal() returned {refusal!r} because MONITOR_ENFORCED is off"
    )


async def test_operator_can_still_release_a_pause_with_the_monitor_off(
    session, monitor_off_settings
):
    """The escape hatch that makes honouring pauses safe.

    Because an existing pause now survives MONITOR_ENFORCED being off, the flag is no
    longer the way out if the monitor ever mass-pauses legitimate customers. The per-org
    operator release must therefore work with the monitor off -- the ops monitoring routes
    are deliberately not flag-gated. Pinned so nobody "tidies up" by gating them.
    """
    from app.auth.security import hash_password
    from app.db.base import set_org_context
    from app.models import User
    from app.services import monitor_score, telephony_access

    org_id = await _approved_but_paused_org(session, monitor_off_settings)

    operator = User(
        id=uuid.uuid4(),
        email="operator@example.com",
        hashed_password=hash_password("correct-horse-battery"),
        full_name="Op",
        is_active=True,
    )
    session.add(operator)
    await session.commit()

    set_org_context(session, org_id)
    state = await monitor_score.get_state(session, org_id, create=False)
    assert state is not None

    await monitor_score.unpause(session, state, operator_id=operator.id, note="false positive")
    await session.commit()

    assert state.level == "normal"
    assert await telephony_access.refusal(session, monitor_off_settings, org_id, "sms") is None
    assert await telephony_access.refusal(session, monitor_off_settings, org_id, "call") is None


async def test_control_the_same_pause_is_honoured_with_the_monitor_on(
    session, monitor_on_settings
):
    """Control: the pause itself is well-formed and the gate does honour it when the
    monitor is enforced. Proves the two tests above are about the flag, not the fixture."""
    from app.services import telephony_access

    org_id = await _approved_but_paused_org(session, monitor_on_settings)

    assert await telephony_access.refusal(session, monitor_on_settings, org_id, "sms") == (
        "account_paused"
    )
    assert await telephony_access.refusal(session, monitor_on_settings, org_id, "call") == (
        "account_paused"
    )
