"""Groq Whisper speech-to-text for call transcripts (user decision 2026-09-27).

whisper-large-v3-turbo measured 16.1% WER vs Deepgram nova-3 on our 20-call bench (local
Parakeet 18.2%, Zipformer 18.5%) at ~$0.0007/audio-min and zero box CPU. Whisper can
invent text over silence or hold music, so segments Whisper itself scores as probably
not speech are dropped (``_keep``).

Returns the stt worker's response shape so the scheduler stores it the same way:
``{"segments": [{"channel", "text", "start_ms"}], "channels", "audio_sec", "cpu_sec"}``.
A dual-channel recording is sent one side at a time (agent = channel 0, customer = 1),
so speaker roles survive; a mixed one is a single channel.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger("groq_stt")

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
TURBO = "whisper-large-v3-turbo"
#: Whisper's own "this was not speech" probability; above it the text is usually invented.
NO_SPEECH_MAX = 0.6
#: Very low confidence together with a raised no-speech score is the other hallucination tell.
LOGPROB_MIN = -1.0
NO_SPEECH_WITH_LOW_LOGPROB = 0.3


class GroqSttError(Exception):
    """Groq refused or failed the request (the job is retried / falls back to local)."""


def enabled(settings) -> bool:  # noqa: ANN001
    key = getattr(settings, "groq_api_key", None)
    return bool(key is not None and key.get_secret_value())


def _keep(seg: dict) -> bool:
    text = (seg.get("text") or "").strip()
    if not text:
        return False
    no_speech = float(seg.get("no_speech_prob") or 0.0)
    logprob = float(seg.get("avg_logprob") or 0.0)
    if no_speech > NO_SPEECH_MAX:
        return False
    if logprob < LOGPROB_MIN and no_speech > NO_SPEECH_WITH_LOW_LOGPROB:
        return False
    return True


async def transcribe_bytes(
    settings, audio: bytes, *, filename: str, content_type: str, client, model: str = TURBO  # noqa: ANN001
) -> tuple[list[dict], float]:
    """One file -> (kept segments with start seconds and text, audio duration seconds)."""
    resp = await client.post(
        URL,
        headers={"Authorization": f"Bearer {settings.groq_api_key.get_secret_value()}"},
        files={"file": (filename, audio, content_type)},
        data={
            "model": model,
            "language": "en",
            "temperature": "0",
            "response_format": "verbose_json",
        },
        timeout=300.0,
    )
    if resp.status_code != 200:
        raise GroqSttError(f"groq {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    raw = data.get("segments") or []
    kept = [s for s in raw if _keep(s)]
    if len(kept) != len(raw):
        log.info("groq_stt_dropped_segments", dropped=len(raw) - len(kept), total=len(raw))
    return kept, float(data.get("duration") or 0.0)


async def transcribe_recording(settings, store, rec, *, client, model: str = TURBO) -> dict:  # noqa: ANN001
    """A stored CallRecording -> the stt worker's response shape."""
    from app.services import recordings

    ext = "mp3" if (rec.content_type or "").endswith("mpeg") else "wav"
    ctype = rec.content_type or "audio/mpeg"
    sides: list[tuple[int, bytes]] = []
    if rec.channel_layout == "dual":
        for channel, layout in ((0, "agent"), (1, "customer")):
            try:
                sides.append((channel, await recordings.load_recording_bytes(store, rec, layout)))
            except KeyError:
                sides = []
                break
    if not sides:
        sides = [(0, await recordings.load_recording_bytes(store, rec))]

    segments: list[dict] = []
    duration = 0.0
    for channel, audio in sides:
        kept, seconds = await transcribe_bytes(
            settings, audio, filename=f"call.{ext}", content_type=ctype, client=client, model=model
        )
        duration = max(duration, seconds)
        for seg in kept:
            segments.append(
                {
                    "channel": channel,
                    "text": (seg.get("text") or "").strip(),
                    "start_ms": int(float(seg.get("start") or 0.0) * 1000),
                }
            )
    segments.sort(key=lambda s: s["start_ms"])
    return {
        "segments": segments,
        "channels": len(sides),
        "audio_sec": duration,
        "cpu_sec": 0,
        "model": model,
    }
