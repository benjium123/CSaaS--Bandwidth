"""P43 live-captions worker: silent live transcription for LiveKit call rooms.

The backend dispatches this worker (agent name ``live-captions``) into a customer
call room when the org pays for live transcription. For every audio track it runs
Silero VAD locally, transcribes utterances with the local STT router, overflows to
Deepgram only when allowed, and publishes caption data topics plus backend transcript
segments. It never announces audio and never publishes audio itself.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import json
import logging
import math
import os
import time
import wave
from collections.abc import Callable
from typing import Any

import httpx

from .backend_client import BackendClient
from .transcript_buffer import Segment
from .worker_config import resolve_idle_processes, role_for_participant

logger = logging.getLogger(__name__)

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8080")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
STT_URL = os.getenv("STT_URL", "http://127.0.0.1:9100")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
AGENT_NAME_DEFAULT = "live-captions"
MAX_CALL_SECONDS = int(os.getenv("LIVE_CAPTIONS_MAX_SECONDS", "7200"))
MIN_UTTERANCE_SECONDS = 0.4
MAX_UTTERANCE_SECONDS = 30.0


def _metadata(ctx: Any) -> dict[str, Any]:
    """Parse the worker metadata, backing out a safe default like call_monitor does."""
    raw = getattr(ctx.job, "metadata", "") or ""
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {"call_id": str(raw).strip()}


def stt_token(secret: str) -> str:
    """Auth token for the local STT service: sha256("stt:" + secret)."""
    return hashlib.sha256(f"stt:{secret}".encode()).hexdigest()


def _wav_from_pcm(pcm: bytes, sample_rate: int, num_channels: int) -> bytes:
    """Wrap 16-bit PCM in a WAV container without importing livekit."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(num_channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm)
    return buf.getvalue()


def utterance_wav(frames: list[Any]) -> bytes:
    """Combine VAD utterance frames into a 16 kHz mono 16-bit WAV.

    ``frames`` may be livekit ``rtc.AudioFrame`` objects or simple fakes with
    ``.data``, ``.sample_rate`` and ``.num_channels``. The common test path is
already 16 kHz mono and avoids importing livekit entirely.
    """
    if not frames:
        return _wav_from_pcm(b"", 16000, 1)

    all_native = all(
        getattr(frame, "sample_rate", None) == 16000
        and getattr(frame, "num_channels", None) == 1
        for frame in frames
    )
    if all_native:
        pcm = b"".join(bytes(frame.data) for frame in frames)
        return _wav_from_pcm(pcm, 16000, 1)

    from livekit import rtc

    combined = rtc.combine_audio_frames(frames)
    if combined.sample_rate != 16000 or combined.num_channels != 1:
        resampler = rtc.AudioResampler(combined.sample_rate, 16000, num_channels=1)
        out_frames: list[Any] = []
        pushed = resampler.push(combined)
        if pushed:
            out_frames.extend(pushed)
        flushed = resampler.flush()
        if flushed:
            out_frames.extend(flushed)
        if out_frames:
            combined = rtc.combine_audio_frames(out_frames)
        else:
            return _wav_from_pcm(b"", 16000, 1)

    return _wav_from_pcm(bytes(combined.data), 16000, 1)


def caption_payload(role: str, text: str, at_ms: int, engine: str) -> bytes:
    """Build the room data payload for a caption."""
    return json.dumps(
        {"type": "caption", "role": role, "text": text, "at_ms": at_ms, "engine": engine}
    ).encode()


def _frames_duration(frames: list[Any]) -> float:
    """Duration in seconds for a list of audio frames."""
    if not frames:
        return 0.0

    total_samples = 0
    for frame in frames:
        samples = getattr(frame, "samples_per_channel", None)
        if samples is None:
            samples = len(getattr(frame, "data", b"")) // 2
        total_samples += int(samples)

    rate = getattr(frames[0], "sample_rate", 16000) or 16000
    return total_samples / float(rate)


def _split_frames(frames: list[Any], max_duration: float) -> list[list[Any]]:
    """Split frames into chunks no longer than ``max_duration`` seconds."""
    if not frames:
        return []

    rate = getattr(frames[0], "sample_rate", 16000) or 16000
    chunks: list[list[Any]] = []
    current: list[Any] = []
    current_samples = 0

    for frame in frames:
        samples = getattr(frame, "samples_per_channel", None)
        if samples is None:
            samples = len(getattr(frame, "data", b"")) // 2
        samples = int(samples)

        if current and (current_samples + samples) / rate > max_duration:
            chunks.append(current)
            current = []
            current_samples = 0

        current.append(frame)
        current_samples += samples

    if current:
        chunks.append(current)
    return chunks


class Router:
    """Transcription router that prefers local STT and overflows to Deepgram."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        stt_url: str,
        api_secret: str,
        overflow: str = "none",
        deepgram_api_key: str = "",
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._stt_url = stt_url.rstrip("/")
        self._api_secret = api_secret
        self._overflow = overflow
        self._deepgram_api_key = deepgram_api_key
        self._now = now
        self._health_cache: tuple[float, dict[str, Any] | None] | None = None
        self.deepgram_seconds = 0.0

    async def _get_health(self) -> dict[str, Any] | None:
        now = self._now()
        if self._health_cache is not None:
            cached_at, cached = self._health_cache
            if now - cached_at < 5.0:
                return cached

        health: dict[str, Any] | None = None
        try:
            response = await self._client.get(f"{self._stt_url}/health", timeout=10.0)
            if response.status_code == 200:
                health = response.json()
        except Exception:
            health = None

        self._health_cache = (now, health)
        return health

    def _health_ok(self, health: dict[str, Any] | None) -> bool:
        if not health:
            return False
        try:
            cpus = float(health["cpus"])
            load1 = float(health["load1"])
            live_busy = float(health["live_busy"])
            live_capacity = float(health["live_capacity"])
        except (KeyError, TypeError, ValueError):
            return False
        if cpus <= 0 or live_capacity <= 0:
            return False
        if load1 / cpus > 0.85:
            return False
        if live_busy >= live_capacity:
            return False
        return True

    async def _transcribe_local(self, wav: bytes) -> str | None:
        url = f"{self._stt_url}/transcribe?engine=zipformer&live=1"
        headers = {"Authorization": f"Bearer {stt_token(self._api_secret)}"}
        try:
            response = await self._client.post(
                url, content=wav, headers=headers, timeout=10.0
            )
            if response.status_code != 200:
                return None
            data = response.json()
            segments = data.get("segments") or []
            return " ".join(
                str(segment.get("text") or "") for segment in segments
            ).strip()
        except Exception:
            return None

    async def _transcribe_deepgram(self, wav: bytes, seconds: float) -> str | None:
        url = "https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true"
        headers = {
            "Authorization": f"Token {self._deepgram_api_key}",
            "Content-Type": "audio/wav",
        }
        try:
            response = await self._client.post(
                url, content=wav, headers=headers, timeout=10.0
            )
            if response.status_code != 200:
                return None
            data = response.json()
            channels = (data.get("results") or {}).get("channels") or []
            if not channels:
                return ""
            alternatives = channels[0].get("alternatives") or []
            if not alternatives:
                return ""
            text = (alternatives[0].get("transcript") or "").strip()
            self.deepgram_seconds += seconds
            return text
        except Exception:
            return None

    async def transcribe(self, wav: bytes, seconds: float) -> tuple[str, str | None]:
        health = await self._get_health()
        if self._health_ok(health):
            local_text = await self._transcribe_local(wav)
            if local_text is not None:
                return (local_text, "local")

        if self._overflow == "deepgram" and self._deepgram_api_key:
            deepgram_text = await self._transcribe_deepgram(wav, seconds)
            if deepgram_text is not None:
                return (deepgram_text, "deepgram")

        return ("", None)


async def entrypoint(ctx: Any) -> None:
    from livekit import rtc
    from livekit.agents import AutoSubscribe

    meta = _metadata(ctx)
    call_id = str(meta.get("call_id") or "")
    if not call_id:
        logger.warning("live-captions dispatched without a call id; leaving")
        ctx.shutdown()
        return

    overflow = str(meta.get("overflow") or "none")
    shared_client = httpx.AsyncClient(timeout=20.0)
    backend = BackendClient(
        BACKEND_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, client=shared_client
    )
    router = Router(
        client=shared_client,
        stt_url=STT_URL,
        api_secret=LIVEKIT_API_SECRET,
        overflow=overflow,
        deepgram_api_key=DEEPGRAM_API_KEY if overflow == "deepgram" else "",
        now=time.monotonic,
    )

    started = time.monotonic()
    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    seen_tracks: set[str] = set()
    pending_retry: list[dict] = []
    finalized = False

    def now_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    def role_for(participant: Any) -> str:
        return role_for_participant(
            kind=getattr(participant, "kind", None),
            attributes=dict(getattr(participant, "attributes", {}) or {}),
            sip_kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
        )

    async def transcribe(track: Any, participant: Any) -> None:
        role = role_for(participant)
        from livekit.agents.vad import VADEventType
        from livekit.plugins import silero

        audio = rtc.AudioStream(track)
        vad = silero.VAD.load(
            min_silence_duration=0.5,
            min_speech_duration=0.2,
            activation_threshold=0.5,
            sample_rate=16000,
        )
        stream = vad.stream()
        resampler = None

        def feed_frame(frame: Any) -> None:
            nonlocal resampler
            if (
                getattr(frame, "sample_rate", None) != 16000
                or getattr(frame, "num_channels", None) != 1
            ):
                if resampler is None:
                    resampler = rtc.AudioResampler(
                        frame.sample_rate, 16000, num_channels=1
                    )
                for out_frame in resampler.push(frame):
                    stream.push_frame(out_frame)
            else:
                stream.push_frame(frame)

        async def pump() -> None:
            try:
                async for event in audio:
                    feed_frame(event.frame)
                if resampler is not None:
                    for out_frame in resampler.flush():
                        stream.push_frame(out_frame)
                stream.end_input()
            except Exception:
                logger.exception(
                    "live-captions audio pump failed call_id=%s role=%s", call_id, role
                )

        async def handle_utterance(frames: list[Any], at_ms: int, seconds: float) -> None:
            try:
                wav = utterance_wav(frames)
                text, engine = await router.transcribe(wav, seconds)
            except Exception:
                logger.exception(
                    "live-captions transcribe failed call_id=%s role=%s", call_id, role
                )
                return

            if not text or not engine:
                return

            try:
                payload = caption_payload(role, text, at_ms, engine)
                await ctx.room.local_participant.publish_data(
                    payload, reliable=True, topic="captions"
                )
            except Exception:
                logger.exception(
                    "live-captions publish failed call_id=%s role=%s", call_id, role
                )

            segment = Segment(role, text, at_ms)
            segment_payload = [dataclasses.asdict(segment)]
            try:
                rejected = await backend.post_transcript(call_id, segment_payload)
            except Exception:
                logger.exception(
                    "live-captions transcript post failed call_id=%s role=%s",
                    call_id,
                    role,
                )
                rejected = segment_payload
            if rejected:
                logger.warning(
                    "live-captions transcript post rejected call_id=%s role=%s segments=%d",
                    call_id,
                    role,
                    len(rejected),
                )
                pending_retry.extend(rejected)

        async def process_utterance(frames: list[Any]) -> None:
            if not frames:
                return
            duration = _frames_duration(frames)
            if duration < MIN_UTTERANCE_SECONDS:
                return

            end_ms = now_ms()
            start_ms = end_ms - int(duration * 1000)
            if start_ms < 0:
                start_ms = 0

            if duration <= MAX_UTTERANCE_SECONDS:
                await handle_utterance(frames, start_ms, duration)
                return

            chunks = _split_frames(frames, MAX_UTTERANCE_SECONDS)
            chunk_start_ms = start_ms
            for chunk in chunks:
                chunk_duration = _frames_duration(chunk)
                await handle_utterance(chunk, chunk_start_ms, chunk_duration)
                chunk_start_ms += int(chunk_duration * 1000)

        pumping = asyncio.create_task(pump())
        try:
            async for event in stream:
                if event.type == VADEventType.END_OF_SPEECH:
                    await process_utterance(list(getattr(event, "frames", [])))
        except Exception:
            logger.exception(
                "live-captions vad failed call_id=%s role=%s", call_id, role
            )
        finally:
            pumping.cancel()
            await stream.aclose()
            await asyncio.gather(pumping, return_exceptions=True)

    @ctx.room.on("track_subscribed")
    def on_track(track: Any, publication: Any, participant: Any) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO or publication.sid in seen_tracks:
            return
        seen_tracks.add(publication.sid)
        tasks.append(asyncio.create_task(transcribe(track, participant)))

    @ctx.room.on("participant_disconnected")
    def on_left(participant: Any) -> None:
        if role_for(participant) == "user":
            stop.set()

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    if not ctx.room.name.startswith("call-"):
        await shared_client.aclose()
        ctx.shutdown()
        return

    for participant in list(ctx.room.remote_participants.values()):
        for publication in list(participant.track_publications.values()):
            if publication.track is not None:
                on_track(publication.track, publication, participant)

    async def finalize() -> None:
        nonlocal finalized
        if finalized:
            return
        finalized = True

        if pending_retry:
            try:
                rejected = await backend.post_transcript(call_id, pending_retry)
                if rejected:
                    logger.warning(
                        "live-captions retry transcript post rejected call_id=%s segments=%d",
                        call_id,
                        len(rejected),
                    )
            except Exception:
                logger.exception(
                    "live-captions retry transcript post failed call_id=%s", call_id
                )

        if router.deepgram_seconds > 0:
            payload = {
                "call_id": call_id,
                "deepgram_seconds": math.ceil(router.deepgram_seconds),
            }
            url = f"{BACKEND_URL}/api/v1/agent/live-usage"
            headers = {"Authorization": f"Bearer {backend._token()}"}
            try:
                await shared_client.post(url, headers=headers, json=payload)
            except Exception:
                logger.exception(
                    "live-captions live-usage post failed call_id=%s", call_id
                )

    ctx.add_shutdown_callback(finalize)

    try:
        await asyncio.wait_for(stop.wait(), timeout=MAX_CALL_SECONDS)
    except asyncio.TimeoutError:
        logger.warning("live-captions max duration reached call_id=%s", call_id)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await finalize()
        await shared_client.aclose()
        ctx.shutdown()


def main() -> None:
    from livekit.agents import WorkerOptions, cli

    raw_name = os.getenv("LIVE_CAPTIONS_AGENT_NAME", AGENT_NAME_DEFAULT).strip()
    name = raw_name or AGENT_NAME_DEFAULT
    logger.info("live-captions worker registering as agent_name=%s", name)
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=name,
            port=8083,
            num_idle_processes=resolve_idle_processes("live-captions"),
        )
    )


if __name__ == "__main__":
    main()
