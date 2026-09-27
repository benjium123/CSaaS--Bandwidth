"""Unit tests for the hold-music worker's pure audio helpers.

``agents.hold_music`` imports livekit lazily inside its entrypoint, so these tests run in a
plain venv with numpy installed and no livekit at all.
"""

from __future__ import annotations

import numpy as np
import pytest

from agents.hold_music import cached_loop, frames, freq, render_loop

SAMPLE_RATE = 48000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
LOOP_SAMPLES = 16 * SAMPLE_RATE


@pytest.fixture(scope="module")
def pcm() -> np.ndarray:
    return render_loop()


def test_freq_is_equal_temperament_a4_440() -> None:
    assert freq(69) == 440
    assert freq(57) == pytest.approx(220.0)
    assert freq(81) == pytest.approx(880.0)


def test_render_loop_is_16s_of_int16(pcm: np.ndarray) -> None:
    assert pcm.dtype == np.int16
    assert pcm.shape == (LOOP_SAMPLES,)


def test_render_loop_peak_is_minus_14_dbfs(pcm: np.ndarray) -> None:
    peak = int(np.max(np.abs(pcm.astype(np.int64))))
    assert peak <= 0.2 * 32767 + 1
    assert peak > 0.1 * 32767


def test_render_loop_seam_is_smooth(pcm: np.ndarray) -> None:
    samples = pcm.astype(np.int64)
    limit = int(0.05 * 32767)
    assert abs(int(samples[-1]) - int(samples[0])) <= limit
    assert int(np.max(np.abs(np.diff(samples[:FRAME_SAMPLES])))) <= limit
    assert int(np.max(np.abs(np.diff(samples[-FRAME_SAMPLES:])))) <= limit


def test_cached_loop_renders_once() -> None:
    assert cached_loop() is cached_loop()
    assert cached_loop().shape == (LOOP_SAMPLES,)


def test_frames_yields_20ms_chunks(pcm: np.ndarray) -> None:
    chunk = next(frames(pcm))
    assert isinstance(chunk, np.ndarray)
    assert chunk.dtype == np.int16
    assert chunk.shape == (FRAME_SAMPLES,)
    assert np.array_equal(chunk, pcm[:FRAME_SAMPLES])


def test_frames_cycles_forever_around_the_loop(pcm: np.ndarray) -> None:
    stream = frames(pcm)
    chunks = [next(stream) for _ in range(LOOP_SAMPLES // FRAME_SAMPLES + 3)]
    assert all(chunk.shape == (FRAME_SAMPLES,) for chunk in chunks)
    # the loop is a whole number of frames long, so the chunk after the wrap is the head
    assert np.array_equal(chunks[0], pcm[:FRAME_SAMPLES])
    assert np.array_equal(chunks[LOOP_SAMPLES // FRAME_SAMPLES], pcm[:FRAME_SAMPLES])


def test_frames_wraps_a_partial_last_chunk(pcm: np.ndarray) -> None:
    short = pcm[:2000]
    stream = frames(short)
    assert np.array_equal(next(stream), short[:960])
    assert np.array_equal(next(stream), short[960:1920])
    wrapped = next(stream)
    assert wrapped.shape == (FRAME_SAMPLES,)
    assert np.array_equal(wrapped, np.concatenate([short[1920:], short[:880]]))


def test_frames_uses_sample_rate_and_frame_ms(pcm: np.ndarray) -> None:
    chunk = next(frames(pcm, sample_rate=16000, frame_ms=20))
    assert chunk.shape == (320,)


def test_frames_rejects_an_empty_loop() -> None:
    with pytest.raises(ValueError):
        next(frames(np.zeros(0, dtype=np.int16)))
