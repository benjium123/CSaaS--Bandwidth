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
