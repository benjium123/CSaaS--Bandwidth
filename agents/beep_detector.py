from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def goertzel_power(pcm: bytes, sample_rate: int, freq_hz: float) -> float:
    num_samples = len(pcm) // 2
    if num_samples < 2:
        return 0.0

    usable = pcm[: num_samples * 2]
    omega = 2.0 * math.pi * freq_hz / sample_rate
    coeff = 2.0 * math.cos(omega)
    s_prev = 0.0
    s_prev2 = 0.0

    for (sample,) in struct.iter_unpack("<h", usable):
        s = sample + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s

    raw_power = s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2
    normalized = (
        2.0
        * raw_power
        / (num_samples * num_samples * 32768.0 * 32768.0)
    )
    return max(0.0, normalized)


def _mean_square(pcm: bytes) -> float:
    num_samples = len(pcm) // 2
    if num_samples == 0:
        return 0.0

    usable = pcm[: num_samples * 2]
    total = 0.0
    for (sample,) in struct.iter_unpack("<h", usable):
        total += sample * sample
    return total / num_samples / 32768.0**2


@dataclass
class BeepEvent:
    at_ms: int
    freq_hz: float
    duration_ms: int


class BeepDetector:
    def __init__(
        self,
        sample_rate: int,
        freqs: tuple[float, ...] = (900.0, 1000.0, 1400.0),
        frame_ms: int = 20,
        min_duration_ms: int = 250,
        power_threshold: float = 0.1,
        purity_ratio: float = 5.0,
    ) -> None:
        self._sample_rate = sample_rate
        self._freqs = freqs
        self._frame_ms = frame_ms
        self._frame_samples = sample_rate * frame_ms // 1000
        self._frame_bytes = self._frame_samples * 2
        self._min_duration_ms = min_duration_ms
        self._power_threshold = power_threshold
        self._purity_ratio = purity_ratio

        self._buffer = bytearray()
        self._frame_start_ms: int | None = None

        self._run_freq: float | None = None
        self._run_start_ms = 0
        self._run_last_end_ms = 0
        self._run_dropout = False

    def feed(self, pcm: bytes, at_ms: int) -> BeepEvent | None:
        if not pcm:
            return None

        if not self._buffer and (self._frame_start_ms is None or at_ms > self._frame_start_ms):
            self._frame_start_ms = at_ms

        self._buffer.extend(pcm)

        while len(self._buffer) >= self._frame_bytes:
            frame = bytes(self._buffer[: self._frame_bytes])
            del self._buffer[: self._frame_bytes]

            start_ms = self._frame_start_ms if self._frame_start_ms is not None else at_ms
            end_ms = start_ms + self._frame_ms
            self._frame_start_ms = end_ms

            event = self._process_frame(frame, start_ms, end_ms)
            if event is not None:
                return event

        return None

    def reset(self) -> None:
        self._buffer.clear()
        self._frame_start_ms = None
        self._reset_run()

    def _process_frame(self, frame: bytes, start_ms: int, end_ms: int) -> BeepEvent | None:
        best_freq, best_power = self._best_tone_power(frame)
        total_energy = _mean_square(frame)
        residual = max(0.0, total_energy - best_power)

        is_tonal = (
            best_power >= self._power_threshold
            and best_power >= self._purity_ratio * residual
        )

        if not is_tonal:
            return self._handle_non_tonal()

        if self._run_freq is None:
            self._start_run(best_freq, start_ms, end_ms)
            return None

        if math.isclose(best_freq, self._run_freq, rel_tol=1e-9, abs_tol=1e-3):
            self._run_last_end_ms = end_ms
            self._run_dropout = False
            return None

        event = self._handle_non_tonal()
        self._start_run(best_freq, start_ms, end_ms)
        return event

    def _best_tone_power(self, frame: bytes) -> tuple[float, float]:
        best_freq = self._freqs[0]
        best_power = goertzel_power(frame, self._sample_rate, best_freq)

        for freq_hz in self._freqs[1:]:
            power = goertzel_power(frame, self._sample_rate, freq_hz)
            if power > best_power:
                best_power = power
                best_freq = freq_hz

        return best_freq, best_power

    def _start_run(self, freq_hz: float, start_ms: int, end_ms: int) -> None:
        self._run_freq = freq_hz
        self._run_start_ms = start_ms
        self._run_last_end_ms = end_ms
        self._run_dropout = False

    def _handle_non_tonal(self) -> BeepEvent | None:
        if self._run_freq is None:
            return None

        duration_ms = self._run_last_end_ms - self._run_start_ms
        if duration_ms >= self._min_duration_ms:
            event = BeepEvent(
                at_ms=self._run_start_ms,
                freq_hz=self._run_freq,
                duration_ms=duration_ms,
            )
            self._reset_run()
            return event

        if self._run_dropout:
            self._reset_run()
        else:
            self._run_dropout = True

        return None

    def _reset_run(self) -> None:
        self._run_freq = None
        self._run_start_ms = 0
        self._run_last_end_ms = 0
        self._run_dropout = False


class VoicemailHeuristic:
    def __init__(
        self,
        greeting_speech_seconds: float = 6.0,
        rms_threshold: float = 500.0,
    ) -> None:
        self._greeting_speech_seconds = greeting_speech_seconds
        self._rms_threshold = rms_threshold
        self._gap_threshold = 0.4

        self._run_start: float | None = None
        self._last_speech_end: float | None = None

    def feed_far(self, pcm: bytes, sample_rate: int, now: float) -> None:
        num_samples = len(pcm) // 2
        if num_samples == 0:
            return

        duration = num_samples / sample_rate
        end = now + duration
        rms = _rms(pcm)

        if rms > self._rms_threshold:
            if self._run_start is None or (
                self._last_speech_end is not None
                and now - self._last_speech_end > self._gap_threshold
            ):
                self._run_start = now
            self._last_speech_end = end
        else:
            if (
                self._run_start is not None
                and self._last_speech_end is not None
                and now - self._last_speech_end > self._gap_threshold
            ):
                self._run_start = None
                self._last_speech_end = None

    def note_our_speech(self, now: float) -> None:
        _ = now
        self._run_start = None
        self._last_speech_end = None

    @property
    def likely_machine(self) -> bool:
        if self._run_start is None or self._last_speech_end is None:
            return False
        return (
            self._last_speech_end - self._run_start
            >= self._greeting_speech_seconds
        )


def _rms(pcm: bytes) -> float:
    num_samples = len(pcm) // 2
    if num_samples == 0:
        return 0.0

    usable = pcm[: num_samples * 2]
    total = 0.0
    for (sample,) in struct.iter_unpack("<h", usable):
        total += sample * sample
    return math.sqrt(total / num_samples)


def synth_tone(
    freq_hz: float,
    duration_ms: int,
    sample_rate: int,
    amplitude: float = 0.5,
) -> bytes:
    num_samples = int(sample_rate * duration_ms / 1000)
    if num_samples <= 0:
        return b""

    out = bytearray(num_samples * 2)
    two_pi_f = 2.0 * math.pi * freq_hz / sample_rate
    for i in range(num_samples):
        sample = int(amplitude * 32767.0 * math.sin(two_pi_f * i))
        struct.pack_into("<h", out, i * 2, sample)
    return bytes(out)


def synth_silence(duration_ms: int, sample_rate: int) -> bytes:
    num_samples = int(sample_rate * duration_ms / 1000)
    return b"\x00\x00" * num_samples
