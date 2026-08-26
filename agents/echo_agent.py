from __future__ import annotations

import asyncio
import logging
import os
import time

from livekit import rtc
from livekit.agents import JobContext, JobRequest, WorkerOptions, cli

from .audio_metrics import PacedPlayback, rms, rt_ratio

logger = logging.getLogger(__name__)

ECHO_MODE = os.environ.get("ECHO_MODE", "continuous").lower()
ECHO_RMS_THRESHOLD = float(os.environ.get("ECHO_RMS_THRESHOLD", "500"))
ECHO_SILENCE_MS = int(os.environ.get("ECHO_SILENCE_MS", "600"))


async def request_fnc(req: JobRequest) -> None:
    room_name = req.room_name or ""
    if not room_name.startswith("call-"):
        await req.reject()
    else:
        await req.accept()


async def wait_for_remote_audio_track(room: rtc.Room) -> rtc.AudioTrack:
    """Find a remote audio track, preferring a SIP participant."""
    loop = asyncio.get_running_loop()
    found = loop.create_future()

    def _scan_existing() -> rtc.AudioTrack | None:
        # Prefer SIP participant by attribute.
        for participant in room.remote_participants.values():
            if not participant.attributes.get("sip.callID"):
                continue
            for publication in participant.track_publications.values():
                if (
                    publication.track
                    and publication.kind == rtc.TrackKind.KIND_AUDIO
                    and not publication.track.is_local
                ):
                    return publication.track

        # Fallback: any remote audio track.
        for participant in room.remote_participants.values():
            for publication in participant.track_publications.values():
                if (
                    publication.track
                    and publication.kind == rtc.TrackKind.KIND_AUDIO
                    and not publication.track.is_local
                ):
                    return publication.track
        return None

    def _on_track_subscribed(track: rtc.Track, *args) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO and not track.is_local and not found.done():
            found.set_result(track)

    room.on("track_subscribed", _on_track_subscribed)

    existing_track = _scan_existing()
    if existing_track is not None:
        return existing_track

    try:
        return await asyncio.wait_for(found, timeout=30)
    except asyncio.TimeoutError:
        raise RuntimeError("No remote audio track found")


async def entrypoint(ctx: JobContext) -> None:
    room_name = ctx.room.name if ctx.room else ""
    if not room_name.startswith("call-"):
        logger.warning("Rejecting non-call room: %s", room_name)
        return

    await ctx.connect(auto_subscribe=rtc.AutoSubscribe.SUBSCRIBE_ALL)
    room = ctx.room

    source = rtc.AudioSource(48000, 1)
    echo_track = rtc.LocalAudioTrack.create_audio_track("echo", source)
    await room.local_participant.publish_track(echo_track)
    logger.info("Published echo audio track")

    remote_track = await wait_for_remote_audio_track(room)
    audio_stream = rtc.AudioStream(remote_track)
    logger.info("Echoing remote audio track")

    start_wall = time.monotonic()
    paced = PacedPlayback(48000)

    frames_echoed = 0
    barge_in_count = 0
    stop_latencies_ms: list[float] = []
    current_utterance: list[rtc.AudioFrame] = []
    silence_ms = 0.0
    playing = False
    playback_start_wall: float | None = None
    resampler: rtc.AudioResampler | None = None
    finalized = False

    def log_summary() -> None:
        nonlocal finalized
        if finalized:
            return
        finalized = True

        now = time.monotonic()
        total_samples = paced.stats.total_samples
        ratio = rt_ratio(total_samples, now - start_wall, 48000)
        mean_stop_latency = (
            sum(stop_latencies_ms) / len(stop_latencies_ms) if stop_latencies_ms else 0.0
        )

        logger.info(
            "Echo call summary: played_frames=%d total_samples=%d rt_ratio=%.3f "
            "underruns=%d max_queue_depth_ms=%.1f avg_queue_depth_ms=%.1f "
            "barge_in_count=%d mean_stop_latency_ms=%.1f",
            frames_echoed,
            total_samples,
            ratio,
            paced.stats.underruns,
            paced.stats.max_queue_depth_ms,
            paced.stats.avg_queue_depth_ms,
            barge_in_count,
            mean_stop_latency,
        )

    room.on("participant_disconnected", lambda *args: log_summary())
    room.on("disconnected", lambda *args: log_summary())

    async def play_back(frames: list[rtc.AudioFrame]) -> None:
        nonlocal playing, playback_start_wall, frames_echoed

        for frame in frames:
            if not playing:
                break
            await source.capture_frame(frame)
            frames_echoed += 1
            paced.push(frame.samples_per_channel, time.monotonic())

            if frame.sample_rate > 0:
                await asyncio.sleep(frame.samples_per_channel / frame.sample_rate)

        playing = False
        playback_start_wall = None

    async for event in audio_stream:
        frame = event.frame

        # Normalize to 48k mono if the SIP bridge delivered anything else.
        if frame.sample_rate != 48000 or frame.num_channels != 1:
            if resampler is None:
                resampler = rtc.AudioResampler(
                    frame.sample_rate,
                    frame.num_channels,
                    48000,
                    1,
                )
            frame = resampler.resample(frame)

        frame_rms = rms(frame.data)

        if ECHO_MODE == "barge":
            if playing:
                if frame_rms > ECHO_RMS_THRESHOLD:
                    stop_wall = time.monotonic()
                    if playback_start_wall is not None:
                        latency_ms = (stop_wall - playback_start_wall) * 1000.0
                        stop_latencies_ms.append(latency_ms)
                    playing = False
                    barge_in_count += 1
                    logger.info(
                        "Barge-in stop latency %.1f ms",
                        stop_latencies_ms[-1] if stop_latencies_ms else 0.0,
                    )
                continue

            if frame_rms > ECHO_RMS_THRESHOLD:
                current_utterance.append(frame)
                silence_ms = 0.0
            else:
                if current_utterance:
                    if frame.sample_rate > 0:
                        duration_ms = (
                            frame.samples_per_channel / frame.sample_rate * 1000.0
                        )
                    else:
                        duration_ms = 0.0
                    silence_ms += duration_ms

                    if silence_ms >= ECHO_SILENCE_MS:
                        utterance = current_utterance
                        current_utterance = []
                        playing = True
                        playback_start_wall = time.monotonic()
                        asyncio.create_task(play_back(utterance))
                        silence_ms = 0.0
                else:
                    silence_ms = 0.0
        else:
            await source.capture_frame(frame)
            frames_echoed += 1
            paced.push(frame.samples_per_channel, time.monotonic())

    log_summary()


def main() -> None:
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            request_fnc=request_fnc,
            agent_name="echo",
        )
    )


if __name__ == "__main__":
    main()
