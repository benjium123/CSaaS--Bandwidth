from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from collections import deque
from typing import Any

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RoomInputOptions,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.agents.metrics import EOUMetrics, InterruptionMetrics, LLMMetrics, TTSMetrics
from livekit.plugins import anthropic, deepgram, elevenlabs, openai, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from .backend_client import BackendClient, format_handoff_summary
from .beep_detector import BeepDetector, VoicemailHeuristic
from .transcript_buffer import TranscriptBuffer, assemble_instructions

logger = logging.getLogger(__name__)

AI_ENDPOINT_MIN_SILENCE = float(os.getenv("AI_ENDPOINT_MIN_SILENCE", "0.5"))
AI_ALLOW_INTERRUPTIONS = (
    os.getenv("AI_ALLOW_INTERRUPTIONS", "1").strip().lower()
    in {"1", "true", "yes", "on"}
)
AI_MAX_CALL_SECONDS = int(os.getenv("AI_MAX_CALL_SECONDS", "900"))
AI_SILENCE_HANGUP_SECONDS = int(os.getenv("AI_SILENCE_HANGUP_SECONDS", "20"))
#: How long a requested-but-not-yet-completed warm handoff is given before the AI gives
#: up waiting for a human to join and takes the conversation back over (finding #1).
AI_HANDOFF_WAIT_SECONDS = float(os.getenv("AI_HANDOFF_WAIT_SECONDS", "120"))
#: How long to wait for a beep after the far-speech heuristic (not the beep detector
#: itself) has already flagged the call as likely-machine before dropping the voicemail
#: message anyway (finding #5) - a real machine that never produces a detectable beep
#: must not hang forever with the AI just listening.
AI_BEEP_WAIT_SECONDS = float(os.getenv("AI_BEEP_WAIT_SECONDS", "8"))
#: Margin added on top of the heuristic's own greeting_speech_seconds before the
#: false-positive "human" AMD confirm is allowed to fire (finding #3) - keeps a short
#: greeting window from letting the confirm race ahead of the heuristic itself.
AI_HUMAN_CONFIRM_SECONDS = float(os.getenv("AI_HUMAN_CONFIRM_SECONDS", "25"))
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


def should_hangup_for_silence(
    now: float,
    last_user_at: float,
    last_agent_at: float,
    handoff_requested: bool,
    handoff_completed: bool,
    silence_seconds: float,
) -> bool:
    """F1/F11: the silence watchdog's hangup predicate, pulled out pure so it is
    unit-testable without livekit. A handoff that has been REQUESTED but not yet
    COMPLETED must suppress the silence hangup entirely - the caller going quiet while
    waiting for a human to join is expected, not a reason to disconnect them."""
    if handoff_requested and not handoff_completed:
        return False
    return now - last_user_at >= silence_seconds and now - last_agent_at >= silence_seconds


def handoff_wait_expired(
    now: float,
    handoff_requested_at: float | None,
    handoff_completed: bool,
    wait_seconds: float,
) -> bool:
    """F1: True once a REQUESTED handoff has been waiting longer than `wait_seconds`
    without completing. `handoff_requested_at` is None whenever no handoff is currently
    outstanding (never requested, or already completed/cleared) - always False then."""
    if handoff_requested_at is None or handoff_completed:
        return False
    return now - handoff_requested_at >= wait_seconds


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


class CsaasAgent(Agent):
    """The Agent subclass function tools are declared on. `@function_tool`-decorated
    methods here are auto-discovered by `Agent.__init__` (via `find_function_tools`) -
    no explicit `tools=[...]` wiring is needed. Each tool's docstring IS its
    description to the LLM, so docstrings are written for the model, not for humans
    reading this file.
    """

    def __init__(
        self,
        instructions: str,
        *,
        backend: BackendClient,
        call_id: str,
        contact_e164: str,
    ) -> None:
        super().__init__(instructions=instructions)
        self._backend = backend
        self._call_id = call_id
        self._contact_e164 = contact_e164
        #: Fed by the entrypoint's existing transcript event handlers (same source as
        #: the TranscriptBuffer) so transfer_to_human can summarize recent context
        #: without re-deriving it from the buffer, which drains on its own schedule.
        self.transcript_tail: deque[tuple[str, str]] = deque(maxlen=6)
        self.handoff_requested = False
        #: monotonic time.time()-comparable timestamp `transfer_to_human` set
        #: `handoff_requested` at; None whenever no handoff is currently outstanding.
        #: Read by `handoff_wait_expired` to detect a handoff nobody ever completed.
        self.handoff_requested_at: float | None = None
        self.end_requested = False
        self.end_reason = ""

    @function_tool
    async def lookup_contact(self) -> str:
        """Look up the caller's contact record in the CRM: their name, any tags on
        their record, and a gist of their most recent message with us. Use this when
        the caller references a prior conversation, asks whether you have their
        information, or you want to personalize the call with their name. Takes no
        arguments - it always looks up the contact on the current call.
        """
        if not self._contact_e164:
            return "No records found."
        contact = await self._backend.get_contact(self._call_id, self._contact_e164)
        if not contact:
            return "No records found."
        name = contact.get("name") or "unknown name"
        tags = contact.get("tags") or []
        last_messages = contact.get("last_messages") or []
        parts = [f"Name: {name}"]
        if tags:
            parts.append("Tags: " + ", ".join(str(tag) for tag in tags))
        if last_messages:
            last = last_messages[-1]
            parts.append(
                f"Last message ({last.get('direction', 'unknown')}): "
                f"{last.get('body', '')}"
            )
        return "; ".join(parts)

    @function_tool
    async def book_appointment(self, when: str, notes: str = "") -> str:
        """Book an appointment for the caller. `when` must be the caller's own words
        for the requested time, passed through VERBATIM (e.g. "tomorrow at 3pm",
        "next Monday morning") - do NOT convert it to an ISO date or a specific
        timestamp yourself; the backend keeps the raw text and a human confirms the
        exact time. `notes` is optional free text describing what the appointment is
        for.
        """
        result = await self._backend.book_appointment(
            self._call_id, self._contact_e164, when, notes
        )
        if not result:
            return (
                "The appointment could not be booked due to a system issue - tell "
                "the caller a human will follow up to confirm the time."
            )
        raw_when = result.get("raw_when", when)
        return f'Appointment requested for "{raw_when}" and is pending confirmation.'

    @function_tool
    async def search_knowledge(self, query: str) -> str:
        """Search the organization's knowledge base before answering a question you
        are not certain about from your instructions alone. `query` should be the
        caller's question or its key terms.
        """
        chunks = await self._backend.kb_search(self._call_id, query)
        if not chunks:
            return "Nothing found in the knowledge base."
        return "\n".join(
            f"- {chunk.get('title', '')}: {chunk.get('text', '')}" for chunk in chunks
        )

    @function_tool
    async def transfer_to_human(self, reason: str) -> str:
        """Transfer the call to a human team member. Use this when the caller
        explicitly asks for a person, has a request outside what you can help with, or
        is frustrated. `reason` is a short explanation for the human of why the call
        is being transferred. Keep talking normally with the caller after calling this
        tool - the transfer happens in the background while a human is notified, and
        you should reassure the caller someone will join shortly rather than saying
        goodbye or ending the call.
        """
        summary = format_handoff_summary(self.transcript_tail)
        ok = await self._backend.post_handoff(self._call_id, reason, summary)
        if not ok:
            logger.error(
                "transfer_to_human: post_handoff failed for call_id=%s", self._call_id
            )
            return (
                "The transfer failed due to a system issue - no human was notified. "
                "Keep helping the caller yourself, or offer to take a message for a "
                "human to follow up later."
            )
        self.handoff_requested = True
        self.handoff_requested_at = time.monotonic()
        return (
            "A human has been notified and will join the call shortly. Reassure the "
            "caller someone is coming - do not say goodbye or end the call."
        )

    @function_tool
    async def end_call(self, reason: str = "") -> str:
        """End the call and disconnect. Only call this AFTER you have already said
        your goodbye to the caller in this same turn - this tool does not speak
        anything itself, it only triggers the disconnect once your goodbye has
        finished playing. `reason` is a short note for the call log (e.g. "caller said
        goodbye", "caller asked to stop calling").
        """
        self.end_requested = True
        self.end_reason = reason
        return ""


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

    contact_e164 = str(context.get("contact_e164") or "")
    direction = str(context.get("direction") or "inbound")
    instructions = assemble_instructions(
        context.get("system_prompt") or "You are a helpful phone assistant.",
        context.get("org_name") or "",
        contact_e164,
        direction,
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

    agent = CsaasAgent(
        instructions,
        backend=backend,
        call_id=call_id,
        contact_e164=contact_e164,
    )

    buffer = TranscriptBuffer()
    stop_event = asyncio.Event()
    call_start = time.monotonic()
    last_user_at = call_start
    last_agent_at = call_start
    turn_count = 0
    latencies_ms: list[float] = []
    interruption_count = 0
    # Per-call state, scoped to this entrypoint invocation like every other `nonlocal`
    # here (turn_count, stop_event, ...) - NOT a true module global, so concurrent calls
    # handled by the same worker process never share it.
    handoff_completed = False
    # EOUMetrics and LLMMetrics for the SAME turn share a speech_id but arrive as two
    # separate "metrics_collected" events; keyed here until the matching TTSMetrics
    # closes out voice-to-voice latency for that turn.
    pending_latency_by_speech: dict[str, float] = {}
    # F3: tiny state machine over the user/agent turn stream that flags the first time a
    # user->agent->user sequence completes (0 = nothing seen; 1 = saw a user turn; 2 =
    # saw an agent turn after that; 3 = saw a second user turn - sequence confirmed).
    # Used by _amd_human_confirm so a lone user utterance (e.g. one word bleeding
    # through a real voicemail greeting) can never look like a live back-and-forth.
    human_confirm_stage = 0
    human_confirm_sequence_seen = False

    def _now_ms() -> int:
        return int((time.monotonic() - call_start) * 1000)

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(ev: Any) -> None:
        nonlocal last_user_at, turn_count, human_confirm_stage, human_confirm_sequence_seen
        if not getattr(ev, "is_final", False):
            return
        text = str(getattr(ev, "transcript", "") or "").strip()
        if not text:
            return
        last_user_at = time.monotonic()
        turn_count += 1
        if human_confirm_stage == 0:
            human_confirm_stage = 1
        elif human_confirm_stage == 2:
            human_confirm_stage = 3
            human_confirm_sequence_seen = True
        buffer.add("user", text, _now_ms(), time.monotonic())
        agent.transcript_tail.append(("user", text))

    @session.on("conversation_item_added")
    def _on_conversation_item_added(ev: Any) -> None:
        nonlocal last_agent_at, human_confirm_stage
        item = getattr(ev, "item", None)
        if item is None or getattr(item, "role", None) != "assistant":
            return
        text = _item_text(item).strip()
        if not text:
            return
        last_agent_at = time.monotonic()
        if human_confirm_stage == 1:
            human_confirm_stage = 2
        buffer.add("agent", text, _now_ms(), time.monotonic())
        agent.transcript_tail.append(("agent", text))
        if voicemail_heuristic is not None:
            voicemail_heuristic.note_our_speech(time.monotonic())

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
        # F1/F11: a requested-but-not-yet-completed handoff must NOT be torn down by
        # the max-call-duration hangup - polling every second past the deadline (rather
        # than a single sleep-then-act) lets that in-progress handoff finish (or time
        # out via _handoff_wait_watchdog) before this actually ends the call.
        while not stop_event.is_set():
            await asyncio.sleep(1)
            if stop_event.is_set():
                return
            if time.monotonic() - call_start < AI_MAX_CALL_SECONDS:
                continue
            if agent.handoff_requested and not handoff_completed:
                continue
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
            try:
                # F7: same reasoning as _end_call_watchdog - let the goodbye actually
                # finish playing before tearing the room down instead of clipping it.
                await session.wait_for_idle()
            except Exception:
                logger.exception(
                    "wait_for_idle failed after max-call goodbye for call_id=%s", call_id
                )
            # ctx.shutdown() alone does not delete the room, so the PSTN leg (via the
            # SIP participant) never actually hangs up without this - EXCEPT when a
            # warm handoff just completed: the room must survive the AI's own
            # departure because the human + caller keep talking in it (see
            # _delete_room_unless_handoff).
            if not handoff_completed:
                await ctx.delete_room()
            stop_event.set()
            ctx.shutdown()
            return

    async def _silence_watchdog() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(1)
            if stop_event.is_set():
                break
            now = time.monotonic()
            if should_hangup_for_silence(
                now,
                last_user_at,
                last_agent_at,
                agent.handoff_requested,
                handoff_completed,
                AI_SILENCE_HANGUP_SECONDS,
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
                try:
                    # F7: same reasoning as _end_call_watchdog - let the goodbye
                    # actually finish playing before tearing the room down.
                    await session.wait_for_idle()
                except Exception:
                    logger.exception(
                        "wait_for_idle failed after silence goodbye for call_id=%s",
                        call_id,
                    )
                if not handoff_completed:
                    await ctx.delete_room()
                stop_event.set()
                ctx.shutdown()
                return

    async def _handoff_wait_watchdog() -> None:
        # F1: separate from both watchdogs above - if a requested handoff never
        # completes within AI_HANDOFF_WAIT_SECONDS, apologize, clear the flag, and let
        # the conversation (and the other watchdogs) resume normally rather than
        # tearing the call down.
        while not stop_event.is_set():
            await asyncio.sleep(1)
            if stop_event.is_set():
                break
            if not handoff_wait_expired(
                time.monotonic(),
                agent.handoff_requested_at,
                handoff_completed,
                AI_HANDOFF_WAIT_SECONDS,
            ):
                continue
            logger.info("handoff wait timed out for call_id=%s", call_id)
            agent.handoff_requested = False
            agent.handoff_requested_at = None
            try:
                await session.say(
                    "I wasn't able to reach a teammate - I'll take a message instead."
                )
            except Exception:
                logger.exception(
                    "failed to say handoff-timeout apology for call_id=%s", call_id
                )

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

    async def _complete_handoff() -> None:
        nonlocal handoff_completed
        if handoff_completed or stop_event.is_set():
            return
        handoff_completed = True
        logger.info("warm handoff completing for call_id=%s", call_id)
        try:
            await session.say(
                "I'm connecting you now - they can see the conversation summary."
            )
        except Exception:
            logger.exception(
                "failed to say handoff intro for call_id=%s", call_id
            )
        # The agent departs WITHOUT deleting the room: the human + caller keep talking
        # in it after this job shuts down (see _delete_room_unless_handoff).
        stop_event.set()
        ctx.shutdown()

    @ctx.room.on("participant_connected")
    def _on_participant_connected(participant: Any) -> None:
        identity = str(getattr(participant, "identity", "") or "")
        if agent.handoff_requested and identity.startswith("user-"):
            asyncio.create_task(_complete_handoff())

    async def _end_call_watchdog() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(0.2)
            if stop_event.is_set():
                break
            if not agent.end_requested:
                continue
            logger.info(
                "end_call tool triggered shutdown for call_id=%s reason=%s",
                call_id,
                agent.end_reason,
            )
            try:
                # The LLM's goodbye is already playing (or about to) by the time this
                # flag is set - wait for it to finish so end_call never clips its own
                # goodbye instead of assuming it is already done.
                await session.wait_for_idle()
            except Exception:
                logger.exception(
                    "wait_for_idle failed before end_call shutdown for call_id=%s",
                    call_id,
                )
            if not handoff_completed:
                await ctx.delete_room()
            stop_event.set()
            ctx.shutdown()
            return

    async def _delete_room_unless_handoff(_reason: str = "") -> None:
        # OVERRIDES the old RoomInputOptions(delete_room_on_close=True) behavior: that
        # deleted the room unconditionally whenever the session closed, which would
        # kill the very call being handed off (the human + caller must keep talking in
        # it after the AI departs). Deletion now always routes through this guard.
        if handoff_completed:
            return
        try:
            await ctx.delete_room()
        except Exception:
            logger.exception(
                "delete_room (shutdown guard) failed for call_id=%s", call_id
            )

    async def _find_sip_audio_track() -> Any:
        for participant in ctx.room.remote_participants.values():
            if not participant.attributes.get("sip.callID"):
                continue
            for publication in participant.track_publications.values():
                if (
                    publication.track is not None
                    and publication.kind == rtc.TrackKind.KIND_AUDIO
                ):
                    return publication.track
        return None

    async def _wait_for_sip_audio_track(timeout: float = 10.0) -> Any:
        existing = await _find_sip_audio_track()
        if existing is not None:
            return existing
        found: asyncio.Future[Any] = asyncio.get_event_loop().create_future()

        def _on_track_subscribed(track: Any, _publication: Any, participant: Any) -> None:
            # F4: rtc.Room emits "track_subscribed" as (track, publication, participant)
            # - verified against the installed livekit-agents SDK's own Room._listen_task
            # (`self.emit("track_subscribed", remote_audio_track, subscribed,
            # subscribed_participant)`), not assumed. Without the `participant` arg this
            # fallback would grab the FIRST audio track subscribed for ANY reason, not
            # necessarily the SIP leg's.
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return
            if not participant.attributes.get("sip.callID"):
                return
            if not found.done():
                found.set_result(track)

        ctx.room.on("track_subscribed", _on_track_subscribed)
        try:
            return await asyncio.wait_for(found, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            ctx.room.off("track_subscribed", _on_track_subscribed)

    amd_result_posted = False
    beep_handled = False
    human_result_posted = False
    # F5: set the instant `_handle_beep` starts running, so `_heuristic_machine_beep_wait`
    # can wake immediately instead of always burning its full AI_BEEP_WAIT_SECONDS timeout
    # when a real beep does arrive after the heuristic already fired.
    beep_detected_event = asyncio.Event()

    async def _post_amd_machine() -> None:
        nonlocal amd_result_posted
        if amd_result_posted:
            return
        amd_result_posted = True
        # Suppress normal conversation once machine-detected: disabling STT input (as
        # opposed to session.interrupt(), which only cancels whatever is playing right
        # now) stops the LLM from generating any further replies to a voicemail
        # greeting/prompt tree - the cleanest mechanism AgentSession 1.7 offers for
        # this (verified: session.input.set_audio_enabled).
        session.input.set_audio_enabled(False)
        try:
            await backend.post_amd(call_id, "machine")
        except Exception:
            logger.exception("post_amd(machine) failed for call_id=%s", call_id)

    async def _voicemail_drop_sequence() -> None:
        # The no-clipping gate: wait 300ms AFTER the beep end (or, for the heuristic
        # timeout path, from the moment we gave up waiting for one) before speaking so
        # the voicemail_message's first syllable isn't eaten by a post-beep ramp-up.
        await asyncio.sleep(0.3)
        message = str(context.get("voicemail_message") or "").strip()
        if message:
            try:
                await session.say(message)
            except Exception:
                logger.exception(
                    "failed to say voicemail_message for call_id=%s", call_id
                )
            try:
                await session.wait_for_idle()
            except Exception:
                logger.exception(
                    "wait_for_idle failed after voicemail_message for call_id=%s",
                    call_id,
                )
        if not handoff_completed:
            await ctx.delete_room()
        stop_event.set()
        ctx.shutdown()

    async def _handle_beep(event: Any) -> None:
        nonlocal beep_handled
        if beep_handled:
            return
        beep_handled = True
        beep_detected_event.set()
        logger.info(
            "voicemail beep detected call_id=%s freq_hz=%s duration_ms=%s",
            call_id,
            event.freq_hz,
            event.duration_ms,
        )
        await _post_amd_machine()
        await _voicemail_drop_sequence()

    async def _heuristic_machine_beep_wait() -> None:
        # F5: the far-speech heuristic (unlike the tone-based BeepDetector) has no
        # opinion on whether a beep ever happens - a real machine whose beep the
        # detector never catches (bad line, unusual tone, etc.) must not just sit there
        # with the AI silently listening for the rest of the call.
        nonlocal beep_handled
        try:
            await asyncio.wait_for(beep_detected_event.wait(), timeout=AI_BEEP_WAIT_SECONDS)
            return  # a real beep arrived - _handle_beep is already running the drop.
        except asyncio.TimeoutError:
            pass
        if beep_handled or stop_event.is_set():
            return
        beep_handled = True
        logger.info(
            "beep wait expired after heuristic machine detection; dropping call_id=%s",
            call_id,
        )
        await _voicemail_drop_sequence()

    async def _amd_human_confirm() -> None:
        # F3: fire the "human" AMD verdict only once ALL of (a) enough time has passed
        # that a real voicemail greeting would be over, (b) the heuristic does not
        # currently think this is a machine (either its own 6s-of-continuous-far-speech
        # signal, or a far-speech run that is still open), and (c) we have seen a real
        # user -> agent -> user back-and-forth (not just one word bleeding through a
        # greeting). Polls every second rather than firing once after a fixed delay so
        # it can catch the moment all three become true, however late that is.
        nonlocal human_result_posted
        if voicemail_heuristic is None:
            return
        # VoicemailHeuristic exposes only ONE public property (far_speech_active) by
        # design (see beep_detector.py) - greeting_speech_seconds is read off the
        # private attribute here rather than adding a second one.
        threshold = max(
            25.0, voicemail_heuristic._greeting_speech_seconds + AI_HUMAN_CONFIRM_SECONDS
        )
        while not stop_event.is_set():
            await asyncio.sleep(1)
            if stop_event.is_set() or amd_result_posted or human_result_posted:
                return
            if time.monotonic() - call_start < threshold:
                continue
            if voicemail_heuristic.likely_machine or voicemail_heuristic.far_speech_active:
                continue
            if turn_count < 2 or not human_confirm_sequence_seen:
                continue
            human_result_posted = True
            try:
                await backend.post_amd(call_id, "human")
            except Exception:
                logger.exception("post_amd(human) failed for call_id=%s", call_id)
            return

    voicemail_heuristic: VoicemailHeuristic | None = (
        VoicemailHeuristic() if direction.lower() == "outbound" else None
    )

    async def _voicemail_tap() -> None:
        track = await _wait_for_sip_audio_track()
        if track is None:
            logger.warning(
                "no SIP audio track found for voicemail tap on call_id=%s", call_id
            )
            return
        audio_stream = rtc.AudioStream(track)
        beep_detector: BeepDetector | None = None
        try:
            async for event in audio_stream:
                if stop_event.is_set() or beep_handled:
                    break
                frame = event.frame
                pcm = bytes(frame.data)
                if beep_detector is None:
                    beep_detector = BeepDetector(sample_rate=frame.sample_rate)
                if voicemail_heuristic is not None:
                    voicemail_heuristic.feed_far(
                        pcm, frame.sample_rate, time.monotonic()
                    )
                beep_event = beep_detector.feed(pcm, _now_ms())
                if beep_event is not None:
                    await _handle_beep(beep_event)
                    break
                if (
                    not amd_result_posted
                    and voicemail_heuristic is not None
                    and voicemail_heuristic.likely_machine
                ):
                    await _post_amd_machine()
                    # F5: bounded wait for a beep now that the heuristic alone has
                    # flagged this as a machine - `not amd_result_posted` above already
                    # guards this from firing more than once per call.
                    tasks.append(asyncio.create_task(_heuristic_machine_beep_wait()))
        except Exception:
            logger.exception("voicemail tap failed for call_id=%s", call_id)
        finally:
            await audio_stream.aclose()

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
    # Replaces RoomInputOptions(delete_room_on_close=True) - see _delete_room_unless_handoff.
    ctx.add_shutdown_callback(_delete_room_unless_handoff)

    flusher_task = asyncio.create_task(_transcript_flusher())
    hard_stop_task = asyncio.create_task(_hard_stop())
    silence_task = asyncio.create_task(_silence_watchdog())
    handoff_wait_task = asyncio.create_task(_handoff_wait_watchdog())
    end_call_task = asyncio.create_task(_end_call_watchdog())
    tasks = [flusher_task, hard_stop_task, silence_task, handoff_wait_task, end_call_task]
    if voicemail_heuristic is not None:
        tasks.append(asyncio.create_task(_voicemail_tap()))
        tasks.append(asyncio.create_task(_amd_human_confirm()))

    try:
        await session.start(
            room=ctx.room,
            agent=agent,
            room_input_options=RoomInputOptions(),
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
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
