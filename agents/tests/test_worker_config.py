"""Tests for agents.worker_config.

This module intentionally does NOT import livekit-agents: worker_config.py exists so the
plain backend venv can verify the worker/backend agent-name contract without pulling in
the livekit SDK.
"""

from agents.worker_config import AGENT_NAME_DEFAULT, resolve_agent_name


def test_the_default_matches_the_backends_dispatch_name() -> None:
    """The backend literal lives in services/assistant_dispatch.py and the two packages
    cannot import each other, so the literal is pinned here."""
    assert resolve_agent_name({}) == "ai-agent"
    assert AGENT_NAME_DEFAULT == "ai-agent"


def test_the_env_var_overrides_the_default() -> None:
    assert resolve_agent_name({"AI_AGENT_NAME": "foo"}) == "foo"


def test_a_blank_env_var_falls_back_to_the_default() -> None:
    assert resolve_agent_name({"AI_AGENT_NAME": "   "}) == "ai-agent"


def test_reads_the_process_environment_by_default(monkeypatch) -> None:
    monkeypatch.setenv("AI_AGENT_NAME", "bar")
    assert resolve_agent_name() == "bar"


# --- P43 call-monitor listener -------------------------------------------------------------
from agents.worker_config import (  # noqa: E402
    MONITOR_AGENT_NAME_DEFAULT,
    resolve_monitor_agent_name,
    role_for_participant,
)


def test_monitor_name_matches_the_backend_dispatch_name() -> None:
    assert resolve_monitor_agent_name({}) == "call-monitor"
    assert MONITOR_AGENT_NAME_DEFAULT == "call-monitor"
    assert resolve_monitor_agent_name({"MONITOR_AGENT_NAME": " "}) == "call-monitor"
    assert resolve_monitor_agent_name({"MONITOR_AGENT_NAME": "listener"}) == "listener"


def test_phone_side_is_user_and_browser_side_is_agent() -> None:
    sip = object()
    assert role_for_participant(kind=sip, attributes={}, sip_kind=sip) == "user"
    assert role_for_participant(kind=None, attributes={"sip.callID": "abc"}, sip_kind=sip) == "user"
    assert role_for_participant(kind="standard", attributes={}, sip_kind=sip) == "agent"
