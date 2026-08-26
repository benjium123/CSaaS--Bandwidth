"""SDK-dependent tests: these construct REAL livekit-agents dataclasses and import
agents.ai_agent, which requires the full livekit-agents[...] extras (deepgram,
elevenlabs, openai, anthropic, silero, turn-detector) to be installed. The backend venv
does not carry livekit at all, so these tests must skip there rather than error - run
them explicitly under .venv-agents to verify they actually pass, not just skip.
"""

from __future__ import annotations

import asyncio
import importlib.util
from unittest.mock import AsyncMock

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
    from livekit import rtc
    from livekit.agents import io
    from livekit.agents.metrics import (
        EOUMetrics,
        InterruptionMetrics,
        LLMMetrics,
        TTSMetrics,
    )
    from livekit.rtc.room import EventTypes

    from agents.ai_agent import CsaasAgent, process_metric
    from agents.backend_client import BackendClient


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


def _make_csaas_agent() -> CsaasAgent:
    backend = BackendClient("http://backend", "key", "secret")
    return CsaasAgent(
        "You are a helpful phone assistant.",
        backend=backend,
        call_id="call-1",
        contact_e164="+15551234567",
    )


def test_csaas_agent_declares_the_five_phase_9_tools() -> None:
    """Phase 9: `@function_tool`-decorated methods on a CsaasAgent subclass must be
    auto-discovered by Agent.__init__ (via find_function_tools) - verified here against
    the REAL Agent base class rather than assumed, since nothing in ai_agent.py wires
    `tools=[...]` explicitly."""
    agent = _make_csaas_agent()
    names = {tool.info.name for tool in agent.tools}
    assert names == {
        "lookup_contact",
        "book_appointment",
        "search_knowledge",
        "transfer_to_human",
        "end_call",
    }


def test_csaas_agent_starts_with_no_handoff_or_end_requested() -> None:
    agent = _make_csaas_agent()
    assert agent.handoff_requested is False
    assert agent.end_requested is False
    assert list(agent.transcript_tail) == []


def test_participant_connected_is_a_real_room_event_type() -> None:
    """The warm-handoff completion handler registers `ctx.room.on("participant_connected",
    ...)` - verify that string is one of the real event names rtc.Room actually emits in
    this SDK version, not a guess."""
    assert "participant_connected" in EventTypes.__args__
    assert "track_subscribed" in EventTypes.__args__


def test_audio_stream_is_constructible_from_a_local_track() -> None:
    """The voicemail-drop tap does `rtc.AudioStream(sip_track)` on the SIP
    participant's remote audio track. A local track is enough to prove the
    constructor/iterator shape is real without a live room connection."""
    source = rtc.AudioSource(sample_rate=8000, num_channels=1)
    track = rtc.LocalAudioTrack.create_audio_track("test", source)
    stream = rtc.AudioStream(track, sample_rate=8000, num_channels=1)
    assert hasattr(stream, "__anext__")
    assert hasattr(stream, "aclose")


def test_agent_session_offers_set_audio_enabled_for_pausing_input() -> None:
    """Voicemail suppression uses `session.input.set_audio_enabled(False)` to stop
    feeding STT input once a machine is detected - verify this is a real method on
    io.AgentInput in the installed SDK (chosen over session.interrupt(), which only
    cancels in-flight speech rather than preventing new replies)."""
    assert hasattr(io.AgentInput, "set_audio_enabled")
    assert callable(io.AgentInput.set_audio_enabled)


def test_track_subscribed_event_args_are_track_publication_participant() -> None:
    """F4: `ctx.room.on("track_subscribed", cb)` must invoke `cb` as
    cb(track, publication, participant) - the voicemail tap's fallback subscriber
    (agents/ai_agent.py::_wait_for_sip_audio_track) reads `participant.attributes` off
    the THIRD positional argument, so the real emit shape matters, not just that the
    event name exists (that's test_participant_connected_is_a_real_room_event_type's
    job). Verified against Room.on's own documented argument list for this event
    (matches the real Room._listen_task emit call: `self.emit("track_subscribed",
    remote_audio_track, subscribed, subscribed_participant)`)."""
    doc = rtc.Room.on.__doc__ or ""
    idx = doc.find('"track_subscribed"')
    assert idx != -1, "track_subscribed is not documented on Room.on in this SDK version"
    segment = doc[idx : idx + 250]
    # Arguments are documented in call order: track, publication (Remote...Publication),
    # then participant.
    track_idx = segment.find("`track`")
    publication_idx = segment.find("`publication`")
    participant_idx = segment.find("`participant")
    assert track_idx != -1 and publication_idx != -1 and participant_idx != -1
    assert track_idx < publication_idx < participant_idx


def test_transfer_to_human_success_sets_handoff_requested_and_timestamp() -> None:
    """F2: on a successful post_handoff, handoff_requested AND handoff_requested_at
    (consumed by handoff_wait_expired) must both be set."""
    agent = _make_csaas_agent()
    agent._backend.post_handoff = AsyncMock(return_value=True)

    result = asyncio.run(agent.transfer_to_human("caller wants a person"))

    assert agent.handoff_requested is True
    assert agent.handoff_requested_at is not None
    assert "notified" in result.lower()


def test_transfer_to_human_failure_does_not_set_handoff_requested() -> None:
    """F2 BLOCKER: on a failed post_handoff (False), handoff_requested must stay False
    and the tool must tell the LLM the transfer failed rather than claim success."""
    agent = _make_csaas_agent()
    agent._backend.post_handoff = AsyncMock(return_value=False)

    result = asyncio.run(agent.transfer_to_human("caller wants a person"))

    assert agent.handoff_requested is False
    assert agent.handoff_requested_at is None
    assert "fail" in result.lower()
