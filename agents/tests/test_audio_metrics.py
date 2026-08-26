from __future__ import annotations

import array

import pytest

from agents.audio_metrics import (
    PacedPlayback,
    rt_ratio,
    tail_energy_ratio,
    utterance_spans,
)
from agents.replay_harness import resample_linear


def test_paced_playback_steady_real_time() -> None:
    sample_rate = 48000
    paced = PacedPlayback(sample_rate)

    now = 0.0
    step = 0.02
    samples_per_frame = int(sample_rate * step)

    for _ in range(50):
        paced.push(samples_per_frame, now)
        now += step

    assert paced.stats.underruns == 0
    assert paced.stats.max_queue_depth_ms < 50.0
    assert rt_ratio(paced.stats.total_samples, now, sample_rate) == pytest.approx(1.0)


def test_paced_playback_bursty_push_underrun_and_depth_when_rt_ratio_one() -> None:
    sample_rate = 48000
    paced = PacedPlayback(sample_rate)
    paced.consume(0.0)

    # One big burst arrives at t=1.0 after one second of silence. The owed
    # one-second drain triggers an underrun before the burst is added.
    paced.push(96000, 1.0)
    assert paced.stats.underruns == 1
    assert paced.stats.max_queue_depth_ms > 1000.0
    assert paced.stats.avg_queue_depth_ms > 500.0

    ratio = rt_ratio(paced.stats.total_samples, 2.0, sample_rate)
    assert ratio == pytest.approx(1.0)


def test_rt_ratio_zero_guard() -> None:
    assert rt_ratio(0, 1.0, 48000) == 0.0
    assert rt_ratio(48000, 0.0, 48000) == 0.0
    assert rt_ratio(48000, 1.0, 0) == 0.0


def test_tail_energy_ratio_drops_detection() -> None:
    sample_rate = 16000
    samples = array.array("h", [1000] * 16000)
    source = samples.tobytes()

    assert tail_energy_ratio(source, source, sample_rate, tail_ms=250) == pytest.approx(1.0)

    tail_samples = int(sample_rate * 250 / 1000)
    returned = (
        source[: -2 * tail_samples]
        + array.array("h", [0] * tail_samples).tobytes()
    )
    ratio = tail_energy_ratio(source, returned, sample_rate, tail_ms=250)
    assert ratio < 0.2


def test_utterance_spans_spans_and_min_windows() -> None:
    profile = [100, 100, 0, 0, 200, 200, 200, 0, 10, 10]

    spans_two = utterance_spans(profile, threshold=50, min_windows=2)
    assert spans_two == [(0, 1), (4, 6)]

    spans_three = utterance_spans(profile, threshold=50, min_windows=3)
    assert spans_three == [(4, 6)]


def test_resample_linear_8k_to_48k_length_and_constant_signal() -> None:
    source_rate = 8000
    target_rate = 48000
    source_pcm = array.array("h", [1000] * 8000).tobytes()

    resampled = resample_linear(source_pcm, source_rate, target_rate)

    expected_samples = round(8000 * target_rate / source_rate)
    assert len(resampled) // 2 == expected_samples

    resampled_samples = array.array("h")
    resampled_samples.frombytes(resampled)
    assert all(value == 1000 for value in resampled_samples)
