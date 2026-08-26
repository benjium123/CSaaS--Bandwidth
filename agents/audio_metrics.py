from __future__ import annotations

import array
import math
from dataclasses import dataclass


@dataclass
class FrameStats:
    total_frames: int = 0
    total_samples: int = 0
    underruns: int = 0
    max_queue_depth_ms: float = 0.0
    sum_queue_depth_ms: float = 0.0
    depth_samples: int = 0

    @property
    def avg_queue_depth_ms(self) -> float:
        if self.depth_samples == 0:
            return 0.0
        return self.sum_queue_depth_ms / self.depth_samples


class PacedPlayback:
    """Models a real-time paced consumer.

    Push PCM sample counts as they arrive. `consume` drains owed samples at the
    configured sample rate. Underruns are counted once per starvation event.
    Queue depth is recorded after each push, separately from throughput ratio.
    """

    def __init__(self, sample_rate: int) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self.sample_rate = sample_rate
        self._buffer_samples = 0.0
        self._last_consume_time: float | None = None
        self.stats = FrameStats()

    def consume(self, now: float) -> None:
        if self._last_consume_time is None:
            self._last_consume_time = now
            return

        elapsed = now - self._last_consume_time
        if elapsed <= 0:
            return

        self._last_consume_time = now
        owed = elapsed * self.sample_rate

        # A deficit smaller than one sample is float drift in the caller's timestamps
        # (0.02s steps are not exact in binary), not starvation.
        if self._buffer_samples + 1.0 >= owed:
            self._buffer_samples = max(0.0, self._buffer_samples - owed)
        else:
            self.stats.underruns += 1
            self._buffer_samples = 0.0

    def push(self, num_samples: int, now_seconds: float) -> None:
        if num_samples < 0:
            raise ValueError("num_samples must be non-negative")

        # Drain anything owed before adding new samples.
        self.consume(now_seconds)

        self._buffer_samples += num_samples
        self.stats.total_frames += 1
        self.stats.total_samples += num_samples

        depth_ms = (self._buffer_samples / self.sample_rate) * 1000.0
        self.stats.sum_queue_depth_ms += depth_ms
        self.stats.depth_samples += 1
        self.stats.max_queue_depth_ms = max(self.stats.max_queue_depth_ms, depth_ms)


def rt_ratio(samples_out: int, wall_seconds: float, sample_rate: int) -> float:
    denominator = wall_seconds * sample_rate
    if denominator <= 0:
        return 0.0
    return samples_out / denominator


def rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    pcm = pcm[: len(pcm) - (len(pcm) % 2)]
    if not pcm:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm)
    if len(samples) == 0:
        return 0.0
    sum_squares = sum(v * v for v in samples)
    return math.sqrt(sum_squares / len(samples))


def tail_energy_ratio(
    source: bytes,
    returned: bytes,
    sample_rate: int,
    tail_ms: int = 250,
) -> float:
    """RMS of returned tail divided by RMS of source tail.

    A pipeline that drops sentence tails shows a ratio << 1.0 while a simple
    throughput ratio still looks fine.
    """
    if sample_rate <= 0 or not source or not returned:
        return 0.0

    tail_bytes = int(sample_rate * (tail_ms / 1000.0)) * 2
    if tail_bytes <= 0:
        return 0.0

    src_tail = source[-tail_bytes:] if len(source) >= tail_bytes else source
    ret_tail = returned[-tail_bytes:] if len(returned) >= tail_bytes else returned

    src_rms = rms(src_tail)
    if src_rms == 0.0:
        return 0.0

    return rms(ret_tail) / src_rms


def energy_profile(
    pcm: bytes,
    sample_rate: int,
    window_ms: int = 20,
) -> list[float]:
    if sample_rate <= 0 or window_ms <= 0:
        return []

    window_samples = int(sample_rate * (window_ms / 1000.0))
    if window_samples <= 0:
        return []

    window_bytes = window_samples * 2
    profile: list[float] = []

    for pos in range(0, len(pcm), window_bytes):
        chunk = pcm[pos : pos + window_bytes]
        if not chunk:
            break
        profile.append(rms(chunk))

    return profile


def utterance_spans(
    profile: list[float],
    threshold: float,
    min_windows: int = 3,
) -> list[tuple[int, int]]:
    if min_windows <= 0:
        min_windows = 1

    spans: list[tuple[int, int]] = []
    start: int | None = None

    for i, energy in enumerate(profile):
        if energy >= threshold:
            if start is None:
                start = i
        else:
            if start is not None:
                length = i - start
                if length >= min_windows:
                    spans.append((start, i - 1))
                start = None

    if start is not None:
        length = len(profile) - start
        if length >= min_windows:
            spans.append((start, len(profile) - 1))

    return spans
