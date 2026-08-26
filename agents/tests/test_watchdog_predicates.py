"""F1/F11: pure predicate functions extracted out of ai_agent.py's entrypoint closures
(silence watchdog + handoff-wait watchdog). Both take only plain floats/bools and touch
no livekit type at all - no AgentSession, no JobContext, no SDK dataclass fixtures like
process_metric's tests need. `agents.ai_agent` itself still does `from livekit import
rtc` at module scope though, so importing it (to reach these two functions) still
requires livekit-agents to be installed - same skip-guard as test_ai_agent_sdk.py, so
this collects cleanly (skipped, not erroring) under the plain backend venv, and actually
runs these two pure functions for real under .venv-agents.
"""

from __future__ import annotations

import importlib.util

import pytest

try:
    livekit_agents_available = importlib.util.find_spec("livekit.agents") is not None
except ModuleNotFoundError:
    livekit_agents_available = False

pytestmark = pytest.mark.skipif(
    not livekit_agents_available,
    reason="livekit-agents (and its plugin extras) are not installed in this venv; "
    "run under .venv-agents to exercise these tests",
)

if livekit_agents_available:
    from agents.ai_agent import handoff_wait_expired, should_hangup_for_silence


# ==================================================================================
# should_hangup_for_silence
# ==================================================================================
def test_hangs_up_when_both_sides_have_been_silent_long_enough() -> None:
    assert should_hangup_for_silence(
        now=100.0,
        last_user_at=70.0,
        last_agent_at=75.0,
        handoff_requested=False,
        handoff_completed=False,
        silence_seconds=20.0,
    )


def test_does_not_hang_up_if_user_spoke_recently() -> None:
    assert not should_hangup_for_silence(
        now=100.0,
        last_user_at=90.0,
        last_agent_at=75.0,
        handoff_requested=False,
        handoff_completed=False,
        silence_seconds=20.0,
    )


def test_does_not_hang_up_if_agent_spoke_recently() -> None:
    assert not should_hangup_for_silence(
        now=100.0,
        last_user_at=70.0,
        last_agent_at=95.0,
        handoff_requested=False,
        handoff_completed=False,
        silence_seconds=20.0,
    )


def test_requested_but_not_completed_handoff_suppresses_silence_hangup() -> None:
    """F1 BLOCKER: even with both sides long silent, a handoff that has been
    REQUESTED but not yet COMPLETED must never trigger the silence hangup path -
    the caller going quiet while waiting for a human to join is expected."""
    assert not should_hangup_for_silence(
        now=100.0,
        last_user_at=0.0,
        last_agent_at=0.0,
        handoff_requested=True,
        handoff_completed=False,
        silence_seconds=20.0,
    )


def test_completed_handoff_no_longer_suppresses_silence_hangup() -> None:
    """Once the handoff COMPLETES, the AI has already departed the room in practice
    (stop_event is set elsewhere) - but the predicate itself must not keep suppressing
    silence hangups forever just because a handoff happened at some point."""
    assert should_hangup_for_silence(
        now=100.0,
        last_user_at=0.0,
        last_agent_at=0.0,
        handoff_requested=True,
        handoff_completed=True,
        silence_seconds=20.0,
    )


def test_silence_boundary_is_inclusive() -> None:
    assert should_hangup_for_silence(
        now=20.0,
        last_user_at=0.0,
        last_agent_at=0.0,
        handoff_requested=False,
        handoff_completed=False,
        silence_seconds=20.0,
    )


# ==================================================================================
# handoff_wait_expired
# ==================================================================================
def test_no_outstanding_handoff_never_expires() -> None:
    assert not handoff_wait_expired(
        now=1000.0, handoff_requested_at=None, handoff_completed=False, wait_seconds=120.0
    )


def test_expired_once_wait_seconds_have_elapsed() -> None:
    assert handoff_wait_expired(
        now=200.0, handoff_requested_at=50.0, handoff_completed=False, wait_seconds=120.0
    )


def test_not_yet_expired_before_wait_seconds_elapse() -> None:
    assert not handoff_wait_expired(
        now=100.0, handoff_requested_at=50.0, handoff_completed=False, wait_seconds=120.0
    )


def test_completed_handoff_never_expires_even_past_the_deadline() -> None:
    assert not handoff_wait_expired(
        now=500.0, handoff_requested_at=50.0, handoff_completed=True, wait_seconds=120.0
    )


def test_wait_boundary_is_inclusive() -> None:
    assert handoff_wait_expired(
        now=170.0, handoff_requested_at=50.0, handoff_completed=False, wait_seconds=120.0
    )
