"""P43 call-monitor worker: a silent listener for monitored softphone calls.

The backend dispatches this worker (agent name ``call-monitor``) into a LiveKit call room
when a call was chosen for safety monitoring (backend services/monitor_calls.py). It:

1. plays the recording/monitoring announcement once the phone side has joined,
2. transcribes every audio track with Deepgram - the phone (SIP) participant is "user",
   the business's people in the browser are "agent",
3. posts the transcript to the backend in batches (the same endpoint the AI agent uses),
4. leaves when the phone side hangs up.

It never speaks otherwise, never publishes video and never changes the call. The backend
reviews the transcript with the safety AI after the call ends.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from typing import Any

from livekit import rtc
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, stt
from livekit.plugins import deepgram, elevenlabs

from .backend_client import BackendClient
from .transcript_buffer import TranscriptBuffer
from .worker_config import (
    resolve_idle_processes,
    resolve_monitor_agent_name,
    role_for_participant,
    sip_call_active,
)

logger = logging.getLogger(__name__)

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8080")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
MONITOR_MAX_CALL_SECONDS = int(os.getenv("MONITOR_MAX_CALL_SECONDS", "7200"))


def _metadata(ctx: JobContext) -> dict[str, Any]:
    raw = getattr(ctx.job, "metadata", "") or ""
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {"call_id": str(raw).strip()}


async def _announce(room: rtc.Room, text: str) -> None:
    """Speak the monitoring announcement into the room, then unpublish."""
    if not text:
        return
    tts = elevenlabs.TTS(model="eleven_flash_v2_5")
    source = rtc.AudioSource(tts.sample_rate, tts.num_channels)
    track = rtc.LocalAudioTrack.create_audio_track("monitor-announcement", source)
    publication = await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    try:
        async with tts.synthesize(text) as stream:
            async for chunk in stream:
                await source.capture_frame(chunk.frame)
        await source.wait_for_playout()
        await asyncio.sleep(0.3)
    except Exception:
        logger.exception("monitor announcement failed")
    finally:
        await room.local_participant.unpublish_track(publication.sid)


async def entrypoint(ctx: JobContext) -> None:
    meta = _metadata(ctx)
    call_id = str(meta.get("call_id") or "")
    if not call_id:
        logger.warning("call-monitor dispatched without a call id; leaving")
        ctx.shutdown()
        return

    backend = BackendClient(BACKEND_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    buffer = TranscriptBuffer()
    started = time.monotonic()
    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    announced = False
    seen_tracks: set[str] = set()
    # Nothing is transcribed until the other side has been told (announcement played).
    ready = asyncio.Event()
    recognizer = deepgram.STT(model="nova-3")

    def now_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    async def transcribe(track: rtc.Track, participant: rtc.RemoteParticipant) -> None:
        role = role_for_participant(
            kind=getattr(participant, "kind", None),
            attributes=dict(getattr(participant, "attributes", {}) or {}),
            sip_kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
        )
        await ready.wait()
        audio = rtc.AudioStream(track)
        stream = recognizer.stream()

        async def pump() -> None:
            async for event in audio:
                stream.push_frame(event.frame)
            stream.end_input()

        pumping = asyncio.create_task(pump())
        try:
            async for event in stream:
                if event.type != stt.SpeechEventType.FINAL_TRANSCRIPT or not event.alternatives:
                    continue
                text = (event.alternatives[0].text or "").strip()
                if text:
                    buffer.add(role, text, now_ms(), time.monotonic())
        except Exception:
            logger.exception("call-monitor transcription failed call_id=%s", call_id)
        finally:
            pumping.cancel()
            await stream.aclose()

    async def post(segments: list[Any]) -> list[Any]:
        payload = [dataclasses.asdict(s) for s in segments]
        try:
            rejected = await backend.post_transcript(call_id, payload)
        except Exception:
            logger.exception("call-monitor transcript post failed call_id=%s", call_id)
            return segments
        return segments[len(segments) - len(rejected) :] if rejected else []

    async def flusher() -> None:
        while not stop.is_set():
            await asyncio.sleep(2)
            if not buffer.due(time.monotonic()):
                continue
            drained = buffer.drain()
            for segment in await post(drained):
                buffer.add(segment.role, segment.text, segment.at_ms, time.monotonic())

    async def final_flush(_reason: str = "") -> None:
        drained = buffer.drain()
        if drained:
            await post(drained)

    def call_answered(participant: rtc.RemoteParticipant) -> bool:
        return sip_call_active(dict(getattr(participant, "attributes", {}) or {}))

    def is_phone(participant: rtc.RemoteParticipant) -> bool:
        return (
            role_for_participant(
                kind=getattr(participant, "kind", None),
                attributes=dict(getattr(participant, "attributes", {}) or {}),
                sip_kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
            )
            == "user"
        )

    async def announce_then_listen(text: str) -> None:
        try:
            await _announce(ctx.room, text)
        finally:
            ready.set()

    def maybe_announce(participant: rtc.RemoteParticipant) -> None:
        nonlocal announced
        if announced or not is_phone(participant) or not call_answered(participant):
            return
        announced = True
        text = str(meta.get("announcement") or "")
        tasks.append(asyncio.create_task(announce_then_listen(text)))

    @ctx.room.on("track_subscribed")
    def on_track(track: rtc.Track, publication: Any, participant: rtc.RemoteParticipant) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO or publication.sid in seen_tracks:
            return
        seen_tracks.add(publication.sid)
        tasks.append(asyncio.create_task(transcribe(track, participant)))
        maybe_announce(participant)

    @ctx.room.on("participant_attributes_changed")
    def on_attributes(_changed: dict, participant: rtc.RemoteParticipant) -> None:
        # livekit-sip publishes the phone's track while it is still ringing; the
        # announcement waits for sip.callStatus to become "active" (answered).
        maybe_announce(participant)

    @ctx.room.on("participant_disconnected")
    def on_left(participant: rtc.RemoteParticipant) -> None:
        if is_phone(participant):
            stop.set()

    # Handlers are registered BEFORE connecting, so no track that arrives while connecting
    # is missed; tracks already in the room are picked up just after.
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    if not ctx.room.name.startswith("call-"):
        ctx.shutdown()
        return
    for participant in list(ctx.room.remote_participants.values()):
        for publication in list(participant.track_publications.values()):
            if publication.track is not None:
                on_track(publication.track, publication, participant)

    ctx.add_shutdown_callback(final_flush)
    tasks.append(asyncio.create_task(flusher()))
    try:
        await asyncio.wait_for(stop.wait(), timeout=MONITOR_MAX_CALL_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("call-monitor max duration reached call_id=%s", call_id)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await final_flush()
        await backend.aclose()
        ctx.shutdown()


def main() -> None:
    name = resolve_monitor_agent_name()
    logger.info("call-monitor worker registering as agent_name=%s", name)
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=name,
            port=8082,
            num_idle_processes=resolve_idle_processes("call-monitor"),
        )
    )


if __name__ == "__main__":
    main()
