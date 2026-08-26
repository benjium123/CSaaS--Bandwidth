"""SDK-dependent tests: these construct REAL livekit-agents dataclasses and import
agents.ai_agent, which requires the full livekit-agents[...] extras (deepgram,
elevenlabs, openai, anthropic, silero, turn-detector) to be installed. The backend venv
does not carry livekit at all, so these tests must skip there rather than error - run
them explicitly under .venv-agents to verify they actually pass, not just skip.
"""

from __future__ import annotations

import importlib.util

import pytest

try:
    # find_spec("livekit.agents") would itself raise ModuleNotFoundError (not return
    # None) when the parent "livekit" package doesn't exist at all - which is exactly
    # the backend venv's situation, so this must be a try/except, not a bare find_spec.
    livekit_agents_available = importlib.util.find_spec("livekit.agents") is not None
except ModuleNotFoundError:
    livekit_agents_available = False

pytestmark = pytest.mark.skipif(
    not livekit_agents_available,
    reason="livekit-agents (and its plugin extras) are not installed in this venv; "
    "run under .venv-agents to exercise these tests",
)

if livekit_agents_available:
    from livekit.agents.metrics import (
        EOUMetrics,
        InterruptionMetrics,
        LLMMetrics,
        TTSMetrics,
    )

    from agents.ai_agent import process_metric


def _eou(speech_id: str, delay: float) -> EOUMetrics:
    return EOUMetrics(
        timestamp=0.0,
        end_of_utterance_delay=delay,
        transcription_delay=0.0,
        on_user_turn_completed_delay=0.0,
        speech_id=speech_id,
    )


def _llm(speech_id: str, ttft: float) -> LLMMetrics:
    return LLMMetrics(
        label="test-llm",
        request_id="req-1",
        timestamp=0.0,
        duration=1.0,
        ttft=ttft,
        cancelled=False,
        completion_tokens=1,
        prompt_tokens=1,
        prompt_cached_tokens=0,
        total_tokens=2,
        tokens_per_second=1.0,
        speech_id=speech_id,
    )


def _tts(speech_id: str, ttfb: float) -> TTSMetrics:
    return TTSMetrics(
        label="test-tts",
        request_id="req-1",
        timestamp=0.0,
        ttfb=ttfb,
        duration=1.0,
        audio_duration=1.0,
        cancelled=False,
        characters_count=10,
        streamed=True,
        speech_id=speech_id,
    )


def _interruption(count: int) -> InterruptionMetrics:
    return InterruptionMetrics(
        timestamp=0.0,
        total_duration=1.0,
        prediction_duration=0.1,
        detection_delay=0.05,
        num_interruptions=count,
        num_backchannels=0,
        num_requests=1,
    )


def test_eou_llm_tts_compose_into_one_voice_to_voice_latency() -> None:
    """F3: EOUMetrics.end_of_utterance_delay + LLMMetrics.ttft + TTSMetrics.ttfb for the
    SAME speech_id must land as exactly one voice-to-voice latency sample, in ms."""
    pending: dict[str, float] = {}
    latencies_ms: list[float] = []

    assert process_metric(_eou("sp-1", 0.20), pending, latencies_ms) == 0
    assert process_metric(_llm("sp-1", 0.30), pending, latencies_ms) == 0
    assert latencies_ms == []  # not closed out until TTS arrives

    delta = process_metric(_tts("sp-1", 0.10), pending, latencies_ms)
    assert delta == 0
    assert latencies_ms == pytest.approx([600.0])
    assert "sp-1" not in pending  # consumed


def test_metrics_for_different_speech_ids_do_not_cross_contaminate() -> None:
    pending: dict[str, float] = {}
    latencies_ms: list[float] = []

    process_metric(_eou("sp-a", 0.1), pending, latencies_ms)
    process_metric(_eou("sp-b", 0.5), pending, latencies_ms)
    process_metric(_llm("sp-a", 0.2), pending, latencies_ms)
    process_metric(_tts("sp-a", 0.05), pending, latencies_ms)

    assert latencies_ms == pytest.approx([350.0])
    # sp-b's EOU is still pending; nothing has closed it out.
    assert pending == {"sp-b": 0.5}


def test_tts_with_no_matching_pending_entry_is_a_silent_no_op() -> None:
    pending: dict[str, float] = {}
    latencies_ms: list[float] = []

    delta = process_metric(_tts("orphan", 0.05), pending, latencies_ms)
    assert delta == 0
    assert latencies_ms == []


def test_interruption_metrics_returns_its_count_as_the_delta() -> None:
    pending: dict[str, float] = {}
    latencies_ms: list[float] = []

    assert process_metric(_interruption(1), pending, latencies_ms) == 1
    assert process_metric(_interruption(3), pending, latencies_ms) == 3
    # Interruption events never touch the latency pipeline.
    assert pending == {}
    assert latencies_ms == []


def test_ai_agent_module_imports_cleanly() -> None:
    """Regression guard for F1/F2/F5/F7/F9: every SDK symbol ai_agent.py imports at
    module scope (elevenlabs.TTS, JobContext.delete_room, AgentSession kwargs,
    JobContext.add_shutdown_callback, MultilingualModel, the metrics classes) must
    actually exist on the installed livekit-agents/plugins - this import is exactly
    what `python -c "import agents.ai_agent"` exercises."""
    import agents.ai_agent  # noqa: F401
