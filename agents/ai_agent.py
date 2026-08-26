from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from typing import Any

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomInputOptions,
    WorkerOptions,
    cli,
)
from livekit.agents.metrics import EOUMetrics, InterruptionMetrics, LLMMetrics, TTSMetrics
from livekit.plugins import anthropic, deepgram, elevenlabs, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from .backend_client import BackendClient
from .transcript_buffer import TranscriptBuffer, assemble_instructions

logger = logging.getLogger(__name__)

AI_ENDPOINT_MIN_SILENCE = float(os.getenv("AI_ENDPOINT_MIN_SILENCE", "0.5"))
AI_ALLOW_INTERRUPTIONS = (
    os.getenv("AI_ALLOW_INTERRUPTIONS", "1").strip().lower()
    in {"1", "true", "yes", "on"}
)
AI_MAX_CALL_SECONDS = int(os.getenv("AI_MAX_CALL_SECONDS", "900"))
AI_SILENCE_HANGUP_SECONDS = int(os.getenv("AI_SILENCE_HANGUP_SECONDS", "20"))
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8080")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = (len(values) - 1) * percentile / 100.0
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    fraction = index - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def process_metric(
    m: Any,
    pending_latency_by_speech: dict[str, float],
    latencies_ms: list[float],
) -> int:
    """Fold one `metrics_collected` payload (`ev.metrics` is ONE metrics object, never a
    list) into the running voice-to-voice latency series and return the interruption
    count delta it represents (0 unless `m` is an InterruptionMetrics). Kept as a pure,
    closure-free function so the EOUMetrics + LLMMetrics + TTSMetrics -> voice-to-voice
    latency composition (end_of_utterance_delay + ttft + ttfb) can be unit-tested
    directly against the real SDK dataclasses (see test_ai_agent_sdk.py) without having
    to stand up a JobContext/AgentSession.
    """
    if isinstance(m, EOUMetrics):
        pending_latency_by_speech[m.speech_id] = m.end_of_utterance_delay
    elif isinstance(m, LLMMetrics):
        pending_latency_by_speech[m.speech_id] = (
            pending_latency_by_speech.get(m.speech_id, 0.0) + m.ttft
        )
    elif isinstance(m, TTSMetrics):
        partial = pending_latency_by_speech.pop(m.speech_id, None)
        if partial is not None:
            latencies_ms.append((partial + m.ttfb) * 1000.0)
    elif isinstance(m, InterruptionMetrics):
        return m.num_interruptions
    return 0


def _item_text(item: Any) -> str:
    content = getattr(item, "content", None)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        text = getattr(part, "text", None)
        if text:
            parts.append(str(text))
    return " ".join(part for part in parts if part)


def _llm_from_context(context: dict[str, Any]) -> Any:
    # Default (and ultimate fallback for an unrecognized provider string) is anthropic
    # claude-haiku per the phase-8 plan: cheap + fast for voice.
    provider = str(
        context.get("llm_provider") or os.getenv("LLM_PROVIDER") or "anthropic"
    ).strip().lower()
    model = context.get("llm_model")
    if provider == "openai":
        return openai.LLM(model=model or "gpt-4o-mini")
    if provider == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            logger.error(
                "LLM_PROVIDER=deepseek but DEEPSEEK_API_KEY is not set; "
                "falling back to anthropic claude-haiku-4-5"
            )
            return anthropic.LLM(model="claude-haiku-4-5")
        # The openai plugin speaks any OpenAI-compatible endpoint; DeepSeek is one.
        return openai.LLM(
            base_url="https://api.deepseek.com",
            model=model or "deepseek-chat",
            api_key=api_key,
        )
    return anthropic.LLM(model=model or "claude-haiku-4-5")


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    if not ctx.room.name.startswith("call-"):
        logger.warning("ignoring non-call room: %s", ctx.room.name)
        ctx.shutdown()
        return

    raw_metadata = getattr(ctx.job, "metadata", "") or ""
    call_id = str(raw_metadata).strip()
    if not call_id:
        logger.warning("empty call UUID metadata; running with defaults")

    backend = BackendClient(
        BACKEND_URL,
        LIVEKIT_API_KEY,
        LIVEKIT_API_SECRET,
    )

    context: dict[str, Any] = {}
    try:
        context = await backend.fetch_context(call_id) or {}
    except Exception:
        logger.exception("failed to fetch context for call_id=%s", call_id)

    instructions = assemble_instructions(
        context.get("system_prompt") or "You are a helpful phone assistant.",
        context.get("org_name") or "",
        str(context.get("contact_e164") or ""),
        context.get("direction") or "inbound",
        context.get("extra_rules") or [],
    )

    llm = _llm_from_context(context)
    # voice_id=None overrides elevenlabs.TTS's own default voice and produces a
    # .../text-to-speech/None/stream 404 (a muted agent) - only pass it when set.
    tts_kwargs: dict[str, Any] = {"model": "eleven_flash_v2_5"}
    if context.get("voice_id"):
        tts_kwargs["voice_id"] = context["voice_id"]
    session = AgentSession(
        stt=deepgram.STT(model="nova-3"),
        llm=llm,
        tts=elevenlabs.TTS(**tts_kwargs),
        # Endpointing moved from VAD's min_silence_duration to the session-level
        # min_endpointing_delay knob; the turn detector model refines end-of-turn
        # detection on top of VAD, so silero VAD keeps its own default here.
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
        min_endpointing_delay=AI_ENDPOINT_MIN_SILENCE,
        allow_interruptions=AI_ALLOW_INTERRUPTIONS,
    )

    buffer = TranscriptBuffer()
    stop_event = asyncio.Event()
    call_start = time.monotonic()
    last_user_at = call_start
    last_agent_at = call_start
    turn_count = 0
    latencies_ms: list[float] = []
    interruption_count = 0
    # EOUMetrics and LLMMetrics for the SAME turn share a speech_id but arrive as two
    # separate "metrics_collected" events; keyed here until the matching TTSMetrics
    # closes out voice-to-voice latency for that turn.
    pending_latency_by_speech: dict[str, float] = {}

    def _now_ms() -> int:
        return int((time.monotonic() - call_start) * 1000)

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(ev: Any) -> None:
        nonlocal last_user_at, turn_count
        if not getattr(ev, "is_final", False):
            return
        text = str(getattr(ev, "transcript", "") or "").strip()
        if not text:
            return
        last_user_at = time.monotonic()
        turn_count += 1
        buffer.add("user", text, _now_ms(), time.monotonic())

    @session.on("conversation_item_added")
    def _on_conversation_item_added(ev: Any) -> None:
        nonlocal last_agent_at
        item = getattr(ev, "item", None)
        if item is None or getattr(item, "role", None) != "assistant":
            return
        text = _item_text(item).strip()
        if not text:
            return
        last_agent_at = time.monotonic()
        buffer.add("agent", text, _now_ms(), time.monotonic())

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: Any) -> None:
        nonlocal interruption_count
        interruption_count += process_metric(
            ev.metrics, pending_latency_by_speech, latencies_ms
        )

    async def _post_transcripts(segments: list[Any]) -> list[Any]:
        """POST `segments`; returns the sublist NOT accepted by the backend (empty if
        everything landed). backend_client chunks internally and, on a failing chunk,
        returns that chunk plus every later one as a contiguous suffix - since `payload`
        below is index-aligned with `segments`, the returned suffix length maps straight
        back onto the tail of `segments`."""
        payload = [dataclasses.asdict(segment) for segment in segments]
        try:
            not_accepted_payloads = await backend.post_transcript(call_id, payload)
        except Exception:
            logger.exception(
                "transcript post failed for call_id=%s", call_id
            )
            return segments
        if not not_accepted_payloads:
            return []
        return segments[len(segments) - len(not_accepted_payloads):]

    async def _transcript_flusher() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(1)
            if stop_event.is_set():
                break
            if not buffer.due(time.monotonic()):
                continue
            drained = buffer.drain()
            if not drained:
                continue
            not_accepted = await _post_transcripts(drained)
            for segment in not_accepted:
                buffer.add(
                    segment.role,
                    segment.text,
                    segment.at_ms,
                    time.monotonic(),
                )

    async def _final_flush(_reason: str = "") -> None:
        # Registered both as the shutdown callback (runs even under cancellation) and as
        # belt-and-braces in the entrypoint's `finally` - must tolerate being called
        # twice. drain() empties the buffer on the first call, so a second call always
        # sees an empty buffer and no-ops here.
        drained = buffer.drain()
        if not drained:
            return
        await _post_transcripts(drained)

    async def _hard_stop() -> None:
        await asyncio.sleep(AI_MAX_CALL_SECONDS)
        if stop_event.is_set():
            return
        logger.info(
            "max call duration reached; ending call_id=%s", call_id
        )
        try:
            await session.say(
                "I'm sorry, but I have to end the call now. Goodbye."
            )
        except Exception:
            logger.exception(
                "failed to say max-call goodbye for call_id=%s", call_id
            )
        # RoomInputOptions(delete_room_on_close=True) only fires when the SESSION
        # closes; ctx.shutdown() alone does not delete the room. Without this the
        # PSTN leg (via the SIP participant) never actually hangs up.
        await ctx.delete_room()
        stop_event.set()
        ctx.shutdown()

    async def _silence_watchdog() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(1)
            if stop_event.is_set():
                break
            now = time.monotonic()
            if (
                now - last_user_at >= AI_SILENCE_HANGUP_SECONDS
                and now - last_agent_at >= AI_SILENCE_HANGUP_SECONDS
            ):
                logger.info(
                    "silence watchdog triggered for call_id=%s", call_id
                )
                try:
                    await session.say(
                        "I haven't heard from you for a while. Goodbye."
                    )
                except Exception:
                    logger.exception(
                        "failed to say silence goodbye for call_id=%s",
                        call_id,
                    )
                await ctx.delete_room()
                stop_event.set()
                ctx.shutdown()
                return

    async def _shutdown_for_sip_disconnect() -> None:
        if stop_event.is_set():
            return
        logger.info(
            "SIP participant disconnected; ending call_id=%s", call_id
        )
        stop_event.set()
        ctx.shutdown()

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(participant: Any) -> None:
        attributes = getattr(participant, "attributes", None) or {}
        if "sip.callID" in attributes:
            asyncio.create_task(_shutdown_for_sip_disconnect())

    @session.on("close")
    def _on_session_close(ev: Any) -> None:
        # Without this, a session that closes on its own (provider error, etc.) with
        # none of our own code having set stop_event leaves the entrypoint parked on
        # `await stop_event.wait()` until the job's own shutdown timeout forces it down
        # - by then the final flush in `finally` may not get to run to completion.
        stop_event.set()

    # Belt-and-braces: runs on ctx shutdown even if the entrypoint task above never
    # reaches its `finally` (e.g. it is cancelled before getting there). _final_flush
    # tolerates being invoked twice.
    ctx.add_shutdown_callback(_final_flush)

    flusher_task = asyncio.create_task(_transcript_flusher())
    hard_stop_task = asyncio.create_task(_hard_stop())
    silence_task = asyncio.create_task(_silence_watchdog())

    try:
        await session.start(
            room=ctx.room,
            agent=Agent(instructions=instructions),
            room_input_options=RoomInputOptions(delete_room_on_close=True),
        )
        if not stop_event.is_set():
            greeting = context.get("greeting")
            if greeting:
                await session.say(str(greeting))
            else:
                await session.generate_reply()
        await stop_event.wait()
    finally:
        stop_event.set()
        for task in (flusher_task, hard_stop_task, silence_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            flusher_task, hard_stop_task, silence_task,
            return_exceptions=True,
        )
        await _final_flush()
        logger.info(
            "call_summary call_id=%s turns=%d latency_p50_ms=%.0f "
            "latency_p95_ms=%.0f interruptions=%d",
            call_id,
            turn_count,
            _percentile(latencies_ms, 50),
            _percentile(latencies_ms, 95),
            interruption_count,
        )
        await backend.aclose()


def main() -> None:
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="ai",
        )
    )


if __name__ == "__main__":
    main()
