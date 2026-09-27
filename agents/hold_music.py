"""Hold-music worker: plays synthesized music into a call room while an agent holds a caller.

The backend dispatches this worker (agent name ``hold-music``) into the room of a live call
when an agent presses Hold. The worker joins without subscribing to anything, tags itself
with the ``csaas.role=hold-music`` participant attribute and publishes a single mono 48 kHz
audio track named ``hold-music``. The music is a calm 16 s loop synthesized in memory - no
audio files and nothing copyrighted. The backend ends the hold by removing this participant;
the worker also leaves by itself after ``HOLD_MUSIC_MAX_SECONDS`` (default 1800) or as soon
as the phone side of the call hangs up.

livekit is imported lazily inside ``entrypoint``/``_play`` so that the pure helpers below
(``render_loop``, ``frames``, ``freq``) can be unit tested without the SDK installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Iterator
from typing import Any

import numpy as np

from .worker_config import resolve_idle_processes, role_for_participant

logger = logging.getLogger(__name__)

AGENT_NAME_DEFAULT = "hold-music"
#: Participant role the backend looks for when it ends a hold.
ROLE = "hold-music"
#: The published track must carry exactly this name.
TRACK_NAME = "hold-music"
SAMPLE_RATE = 48000
NUM_CHANNELS = 1
FRAME_MS = 20
MAX_SECONDS = int(os.getenv("HOLD_MUSIC_MAX_SECONDS", "1800"))
HOLD_POLL_SECONDS = 2.0
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8080")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")

# --- the synthesized loop ------------------------------------------------------
#: Cmaj7, Am7, Fmaj7, G6 as MIDI notes.
CHORDS: tuple[tuple[int, ...], ...] = (
    (60, 64, 67, 71),
    (57, 60, 64, 67),
    (53, 57, 60, 64),
    (55, 59, 62, 64),
)
CHORD_SECONDS = 4.0
LOOP_SECONDS = CHORD_SECONDS * len(CHORDS)
ATTACK_SECONDS = 0.4
RELEASE_SECONDS = 0.8
HARMONIC_2_GAIN = 0.25
ARPEGGIO_STEP_SECONDS = 0.5
ARPEGGIO_ATTACK_SECONDS = 0.05
ARPEGGIO_RELEASE_SECONDS = 0.05
ARPEGGIO_DECAY_SECONDS = 0.45
#: e ** -(ARPEGGIO_DECAY_SECONDS / tau) ~ 2%: the pluck is nearly silent when released.
ARPEGGIO_DECAY_TAU = 0.12
ARPEGGIO_HARMONIC_3_GAIN = 1.0 / 9.0
CROSSFADE_SECONDS = 0.02
#: -14 dBFS: the peak the rendered loop is normalised to (0.2 * 32767).
PEAK_AMPLITUDE = 0.2 * 32767

_LOOP_CACHE: dict[int, np.ndarray] = {}


def freq(midi: float) -> float:
    """Equal-temperament frequency for a MIDI note, A4 (69) = 440 Hz."""
    return 440.0 * 2.0 ** ((midi - 69) / 12.0)


def _envelope(length: int, sample_rate: int, attack: float, release: float) -> np.ndarray:
    """Raised-cosine attack/release envelope over ``length`` samples."""
    env = np.ones(length, dtype=np.float64)
    attack_samples = min(int(round(attack * sample_rate)), length)
    release_samples = min(int(round(release * sample_rate)), length)
    if attack_samples > 1:
        env[:attack_samples] = 0.5 * (
            1.0 - np.cos(np.pi * np.linspace(0.0, 1.0, attack_samples))
        )
    if release_samples > 1:
        env[length - release_samples :] = 0.5 * (
            1.0 + np.cos(np.pi * np.linspace(0.0, 1.0, release_samples))
        )
    return env


def _pad_note(midi: float, length: int, sample_rate: int) -> np.ndarray:
    """Sine fundamental plus a quiet 2nd harmonic: one chord voice."""
    t = np.arange(length, dtype=np.float64) / sample_rate
    base = freq(midi)
    return np.sin(2.0 * np.pi * base * t) + HARMONIC_2_GAIN * np.sin(
        4.0 * np.pi * base * t
    )


def _arpeggio_note(midi: float, length: int, sample_rate: int) -> np.ndarray:
    """Plucked triangle-ish tone: fundamental plus 1/9 of the 3rd harmonic."""
    t = np.arange(length, dtype=np.float64) / sample_rate
    base = freq(midi)
    tone = np.sin(2.0 * np.pi * base * t) + ARPEGGIO_HARMONIC_3_GAIN * np.sin(
        6.0 * np.pi * base * t
    )
    attack = 0.5 * (
        1.0 - np.cos(np.pi * np.clip(t / ARPEGGIO_ATTACK_SECONDS, 0.0, 1.0))
    )
    decay = np.exp(-t / ARPEGGIO_DECAY_TAU)
    remaining = length / float(sample_rate) - t
    release = np.clip(remaining / ARPEGGIO_RELEASE_SECONDS, 0.0, 1.0)
    return tone * attack * decay * release


def render_loop(sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Synthesize the calm 16 s hold-music loop as mono int16 PCM.

    Four 4 s chords, each a sum of sine partials with a soft raised-cosine attack and
    release, plus an arpeggio of one chord note every 0.5 s an octave up. The envelopes
    and the pluck tails run across the chord boundaries so nothing clicks. The result is
    normalised to -14 dBFS and the last 20 ms are crossfaded into the head so the loop
    can be played round and round without a seam.
    """
    total = int(round(sample_rate * LOOP_SECONDS))
    chord_samples = int(round(sample_rate * CHORD_SECONDS))
    step_samples = max(1, int(round(sample_rate * ARPEGGIO_STEP_SECONDS)))
    out = np.zeros(total, dtype=np.float64)

    for index, chord in enumerate(CHORDS):
        start = index * chord_samples
        if start >= total:
            break
        length = min(chord_samples, total - start)

        pad = np.zeros(length, dtype=np.float64)
        for midi in chord:
            pad += _pad_note(midi, length, sample_rate)
        out[start : start + length] += pad * _envelope(
            length, sample_rate, ATTACK_SECONDS, RELEASE_SECONDS
        )

        position = 0
        note = 0
        while position < length:
            note_length = min(step_samples, length - position)
            midi = chord[note % len(chord)] + 12
            out[start + position : start + position + note_length] += _arpeggio_note(
                midi, note_length, sample_rate
            )
            position += note_length
            note += 1

    crossfade = int(round(sample_rate * CROSSFADE_SECONDS))
    if 0 < crossfade < total:
        fade_in = np.linspace(0.0, 1.0, crossfade)
        out[:crossfade] = out[:crossfade] * fade_in + out[-crossfade:] * (1.0 - fade_in)

    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 0.0:
        out *= PEAK_AMPLITUDE / peak
    return np.clip(np.round(out), -32768.0, 32767.0).astype(np.int16)


def cached_loop(sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Render the loop once per process; one worker process serves many jobs."""
    loop = _LOOP_CACHE.get(sample_rate)
    if loop is None:
        loop = render_loop(sample_rate)
        _LOOP_CACHE[sample_rate] = loop
    return loop


def frames(
    pcm: np.ndarray, sample_rate: int = SAMPLE_RATE, frame_ms: int = FRAME_MS
) -> Iterator[np.ndarray]:
    """Yield int16 chunks of exactly ``sample_rate * frame_ms / 1000`` samples, for ever.

    The chunks walk through ``pcm`` and wrap around the end, so the caller can play the
    loop indefinitely. Chunks that cross the loop's end are concatenated into one frame.
    """
    size = int(sample_rate * frame_ms / 1000)
    if size <= 0:
        raise ValueError("frame size must be positive")
    loop = np.asarray(pcm)
    total = int(loop.shape[0])
    if total == 0:
        raise ValueError("pcm must not be empty")

    start = 0
    while True:
        end = start + size
        if end <= total:
            yield loop[start:end]
        else:
            parts = [loop[start:total]]
            remaining = size - (total - start)
            while remaining > 0:
                take = min(remaining, total)
                parts.append(loop[:take])
                remaining -= take
            yield np.concatenate(parts)
        start = (start + size) % total


def _metadata(ctx: Any) -> dict[str, Any]:
    """Parse the worker metadata, backing out a safe default like call_monitor does."""
    raw = getattr(ctx.job, "metadata", "") or ""
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {"call_id": str(raw).strip()}


async def _play(source: Any, pcm: np.ndarray, stop: asyncio.Event) -> None:
    """Push one 20 ms frame per ``capture_frame`` call; the AudioSource paces the send."""
    from livekit import rtc

    for chunk in frames(pcm, SAMPLE_RATE, FRAME_MS):
        if stop.is_set():
            return
        await source.capture_frame(
            rtc.AudioFrame(
                data=chunk.tobytes(),
                sample_rate=SAMPLE_RATE,
                num_channels=NUM_CHANNELS,
                samples_per_channel=int(chunk.shape[0]),
            )
        )


async def entrypoint(ctx: Any) -> None:
    from livekit import rtc
    from livekit.agents import AutoSubscribe

    meta = _metadata(ctx)
    call_id = str(meta.get("call_id") or "")
    room_name = getattr(ctx.room, "name", "")
    pcm = cached_loop(SAMPLE_RATE)

    stop = asyncio.Event()
    reason = "removed"

    def is_phone(participant: Any) -> bool:
        return (
            role_for_participant(
                kind=getattr(participant, "kind", None),
                attributes=dict(getattr(participant, "attributes", {}) or {}),
                sip_kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
            )
            == "user"
        )

    @ctx.room.on("participant_disconnected")
    def on_left(participant: Any) -> None:
        nonlocal reason
        if is_phone(participant):
            reason = "phone left"
            stop.set()

    @ctx.room.on("disconnected")
    def on_disconnected(*_args: Any) -> None:
        # The backend ends a hold by removing this participant from the room.
        stop.set()

    async def watch_hold_state() -> None:
        """Leave as soon as the backend says the call is off hold - covers a Resume pressed
        before this worker had joined (nothing for the backend to remove yet)."""
        nonlocal reason
        import httpx

        from .backend_client import BackendClient

        backend = BackendClient(BACKEND_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                while not stop.is_set():
                    await asyncio.sleep(HOLD_POLL_SECONDS)
                    try:
                        resp = await client.get(
                            f"{BACKEND_URL}/api/v1/agent/hold/{call_id}",
                            headers={"Authorization": f"Bearer {backend._token()}"},
                        )
                        if resp.status_code == 200 and not resp.json().get("on_hold"):
                            reason = "resumed"
                            stop.set()
                    except Exception:  # noqa: BLE001 - a missed poll just waits for the next
                        logger.warning("hold-music state poll failed call_id=%s", call_id)
        finally:
            await backend.aclose()

    playing: asyncio.Task[None] | None = None
    watcher: asyncio.Task[None] | None = None
    try:
        await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_NONE)
        await ctx.room.local_participant.set_attributes({"csaas.role": ROLE})

        source = rtc.AudioSource(SAMPLE_RATE, NUM_CHANNELS)
        track = rtc.LocalAudioTrack.create_audio_track(TRACK_NAME, source)
        await ctx.room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        logger.info("hold-music started call_id=%s room=%s", call_id, room_name)

        playing = asyncio.create_task(_play(source, pcm, stop))
        if call_id:
            watcher = asyncio.create_task(watch_hold_state())
        try:
            await asyncio.wait_for(stop.wait(), timeout=MAX_SECONDS)
        except asyncio.TimeoutError:
            reason = "max duration"
    except asyncio.CancelledError:
        # the backend removes this participant to end the hold
        reason = "removed"
        raise
    except Exception:
        reason = "error"
        logger.exception("hold-music failed call_id=%s room=%s", call_id, room_name)
    finally:
        stop.set()
        for task in (playing, watcher):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        logger.info(
            "hold-music exiting call_id=%s room=%s reason=%s",
            call_id,
            room_name,
            reason,
        )
        ctx.shutdown()


def main() -> None:
    from livekit.agents import WorkerOptions, cli

    raw_name = os.getenv("HOLD_MUSIC_AGENT_NAME", AGENT_NAME_DEFAULT).strip()
    name = raw_name or AGENT_NAME_DEFAULT
    logger.info("hold-music worker registering as agent_name=%s", name)
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=name,
            port=8084,
            num_idle_processes=resolve_idle_processes("hold-music"),
        )
    )


if __name__ == "__main__":
    main()
