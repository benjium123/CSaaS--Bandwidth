from __future__ import annotations

import argparse
import array
import asyncio
import json
import logging
import pathlib
import sys
import time
import wave

from .audio_metrics import (
    PacedPlayback,
    energy_profile,
    rt_ratio,
    tail_energy_ratio,
    utterance_spans,
)

logger = logging.getLogger(__name__)


def resample_linear(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear resampling for 16-bit mono PCM. Pure stdlib."""
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("sample rates must be positive")
    if src_rate == dst_rate or not pcm:
        return pcm

    src = array.array("h")
    src.frombytes(pcm)
    if len(src) < 2:
        return pcm

    dst_count = max(1, round(len(src) * dst_rate / src_rate))
    dst = array.array("h")

    for i in range(dst_count):
        if dst_count == 1:
            pos = 0.0
        else:
            pos = i * (len(src) - 1) / (dst_count - 1)
        idx0 = int(pos)
        frac = pos - idx0
        idx1 = min(idx0 + 1, len(src) - 1)

        value = src[idx0] * (1.0 - frac) + src[idx1] * frac
        dst.append(round(value))

    return dst.tobytes()


def generate_token(api_key: str, api_secret: str, room: str) -> str:
    import jwt

    now = int(time.time())
    payload = {
        "iss": api_key,
        "sub": "replay-harness",
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": True,
            "canSubscribe": True,
        },
        "exp": now + 3600,
    }
    return jwt.encode(payload, api_secret, algorithm="HS256")


async def run(args: argparse.Namespace) -> int:
    from livekit import rtc

    token = generate_token(args.api_key, args.api_secret, args.room)

    room = rtc.Room()
    await room.connect(args.url, token, options=rtc.RoomOptions(auto_subscribe=True))
    logger.info("Connected to room %s", args.room)

    with wave.open(args.wav, "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        source_rate = wav_file.getframerate()
        raw_pcm = wav_file.readframes(wav_file.getnframes())

    if channels != 1 or sample_width != 2:
        raise SystemExit("WAV must be 16-bit PCM mono")

    source_pcm = resample_linear(raw_pcm, source_rate, 48000)
    source_duration = len(source_pcm) / (2 * 48000)
    logger.info(
        "Loaded WAV: %.2fs, %d samples at 48kHz",
        source_duration,
        len(source_pcm) // 2,
    )

    source = rtc.AudioSource(48000, 1)
    replay_track = rtc.LocalAudioTrack.create_audio_track("replay", source)
    await room.local_participant.publish_track(replay_track)
    logger.info("Published replay audio track")

    time.monotonic()
    paced = PacedPlayback(48000)
    remote_buffer = bytearray()
    record_tasks: list[asyncio.Task] = []
    first_remote_time: float | None = None
    last_remote_time: float | None = None

    async def record_track(
        track: rtc.AudioTrack,
        paced_playback: PacedPlayback,
        buffer: bytearray,
    ) -> None:
        nonlocal first_remote_time, last_remote_time

        stream = rtc.AudioStream(track)
        resampler: rtc.AudioResampler | None = None

        async for event in stream:
            frame = event.frame

            if frame.num_channels != 1 or frame.sample_rate != 48000:
                if resampler is None:
                    resampler = rtc.AudioResampler(
                        frame.sample_rate,
                        frame.num_channels,
                        48000,
                        1,
                    )
                frame = resampler.resample(frame)

            now = time.monotonic()
            if first_remote_time is None:
                first_remote_time = now
            last_remote_time = now

            buffer.extend(frame.data)
            paced_playback.push(frame.samples_per_channel, now)

    def on_track_subscribed(track: rtc.Track, *args) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO and not track.is_local:
            task = asyncio.create_task(record_track(track, paced, remote_buffer))
            record_tasks.append(task)

    room.on("track_subscribed", on_track_subscribed)

    # Existing remote tracks may already be present when joining after the agent.
    for participant in room.remote_participants.values():
        for publication in participant.track_publications.values():
            if (
                publication.track
                and publication.kind == rtc.TrackKind.KIND_AUDIO
                and not publication.track.is_local
            ):
                task = asyncio.create_task(
                    record_track(publication.track, paced, remote_buffer)
                )
                record_tasks.append(task)

    async def send_audio(frames: bytes, audio_source: rtc.AudioSource) -> None:
        chunk_samples = 960  # 20ms at 48kHz
        chunk_bytes = chunk_samples * 2

        for offset in range(0, len(frames), chunk_bytes):
            chunk = frames[offset : offset + chunk_bytes]
            if not chunk:
                break

            frame = rtc.AudioFrame(
                data=chunk,
                sample_rate=48000,
                num_channels=1,
                samples_per_channel=len(chunk) // 2,
            )
            await audio_source.capture_frame(frame)
            await asyncio.sleep(len(chunk) / (2 * 48000))

    logger.info("Starting real-time paced playback")
    await send_audio(source_pcm, source)

    logger.info("Playback complete; draining for 3s")
    await asyncio.sleep(3.0)

    for task in record_tasks:
        task.cancel()
    if record_tasks:
        await asyncio.gather(*record_tasks, return_exceptions=True)

    if first_remote_time is not None and last_remote_time is not None:
        active_wall = max(0.0, last_remote_time - first_remote_time)
    else:
        active_wall = 0.0

    returned_bytes = bytes(remote_buffer)
    returned_samples = len(returned_bytes) // 2

    ratio = rt_ratio(returned_samples, active_wall, 48000) if active_wall > 0 else 0.0
    tail_ratio = tail_energy_ratio(source_pcm, returned_bytes, 48000, tail_ms=250)

    source_profile = energy_profile(source_pcm, 48000, window_ms=20)
    returned_profile = energy_profile(returned_bytes, 48000, window_ms=20)

    sent_utterances = len(
        utterance_spans(source_profile, threshold=300.0, min_windows=3)
    )
    returned_utterances = len(
        utterance_spans(returned_profile, threshold=300.0, min_windows=3)
    )

    duration_seconds = active_wall
    report = {
        "rt_ratio": ratio,
        "underruns": paced.stats.underruns,
        "max_queue_depth_ms": paced.stats.max_queue_depth_ms,
        "avg_queue_depth_ms": paced.stats.avg_queue_depth_ms,
        "tail_energy_ratio": tail_ratio,
        "utterances_sent": sent_utterances,
        "utterances_returned": returned_utterances,
        "duration_seconds": duration_seconds,
    }

    print("Replay harness report:")
    for key, value in report.items():
        if isinstance(value, float):
            print(f"  {key}: {value:.3f}")
        else:
            print(f"  {key}: {value}")

    if args.report:
        # One small write at the very end of the run; a thread offload would be noise.
        text = json.dumps(report, indent=2)
        pathlib.Path(args.report).write_text(text, encoding="utf-8")
        print(f"Report written to {args.report}")

    if args.expect_echo:
        passed = (
            report["rt_ratio"] >= 0.97
            and report["underruns"] == 0
            and report["tail_energy_ratio"] >= 0.5
            and report["utterances_returned"] >= report["utterances_sent"]
        )
        if not passed:
            logger.error("Expected echo gate FAILED: %s", report)
            return 1

    await room.disconnect()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay harness for agent audio loops"
    )
    parser.add_argument("--url", required=True, help="LiveKit WS URL")
    parser.add_argument("--api-key", required=True, help="LiveKit API key")
    parser.add_argument("--api-secret", required=True, help="LiveKit API secret")
    parser.add_argument("--room", required=True, help="Room to join")
    parser.add_argument("--wav", required=True, help="16-bit PCM mono WAV file")
    parser.add_argument("--report", help="Optional JSON report output path")
    parser.add_argument(
        "--expect-echo",
        action="store_true",
        help="Fail unless echo gate passes",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    code = asyncio.run(run(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
