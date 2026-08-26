from __future__ import annotations

import random
import struct

import pytest

from agents.beep_detector import (
    BeepDetector,
    VoicemailHeuristic,
    goertzel_power,
    synth_silence,
    synth_tone,
)


def test_goertzel_power_tone_and_silence() -> None:
    sample_rate = 8000
    tone = synth_tone(1000.0, 100, sample_rate)

    tone_power = goertzel_power(tone, sample_rate, 1000.0)

    assert tone_power == pytest.approx(0.125, abs=0.02)
    assert goertzel_power(tone, sample_rate, 2000.0) < 0.01
    assert goertzel_power(synth_silence(100, sample_rate), sample_rate, 1000.0) == 0.0
    assert goertzel_power(b"", sample_rate, 1000.0) == 0.0
    assert goertzel_power(b"\x00", sample_rate, 1000.0) == 0.0


def test_beep_detector_detects_400ms_tone() -> None:
    sample_rate = 8000
    detector = BeepDetector(sample_rate=sample_rate, freqs=(1000.0,))
    signal = synth_tone(1000.0, 400, sample_rate) + synth_silence(
        200, sample_rate
    )

    event = detector.feed(signal, 0)

    assert event is not None
    assert event.freq_hz == 1000.0
    assert event.at_ms == 0
    assert event.duration_ms >= 350


def test_beep_detector_ignores_short_blip() -> None:
    sample_rate = 8000
    detector = BeepDetector(sample_rate=sample_rate, freqs=(1000.0,))
    signal = synth_tone(1000.0, 100, sample_rate) + synth_silence(
        200, sample_rate
    )

    assert detector.feed(signal, 0) is None


def test_beep_detector_ignores_noise() -> None:
    sample_rate = 8000
    rng = random.Random(42)
    num_samples = int(sample_rate * 0.5)
    samples = [int(rng.uniform(-20000, 20000)) for _ in range(num_samples)]
    noise = b"".join(struct.pack("<h", sample) for sample in samples)

    detector = BeepDetector(sample_rate=sample_rate)

    assert detector.feed(noise, 0) is None


def test_beep_detector_two_beeps_across_feeds() -> None:
    sample_rate = 8000
    detector = BeepDetector(sample_rate=sample_rate, freqs=(1000.0,))
    tone = synth_tone(1000.0, 300, sample_rate)
    silence = synth_silence(100, sample_rate)

    first_event = detector.feed(tone + silence, 0)
    second_event = detector.feed(tone + silence, 400)

    assert first_event is not None
    assert second_event is not None
    assert first_event.freq_hz == 1000.0
    assert second_event.freq_hz == 1000.0
    assert second_event.at_ms == 400


def test_beep_detector_chunk_size_independence() -> None:
    sample_rate = 8000
    signal = synth_tone(1000.0, 400, sample_rate) + synth_silence(
        100, sample_rate
    )

    direct_detector = BeepDetector(sample_rate=sample_rate, freqs=(1000.0,))
    direct_event = direct_detector.feed(signal, 0)

    chunked_detector = BeepDetector(sample_rate=sample_rate, freqs=(1000.0,))
    chunked_event = None
    for offset in range(0, len(signal), 7):
        chunked_event = chunked_detector.feed(signal[offset : offset + 7], 0)
        if chunked_event is not None:
            break

    assert direct_event is not None
    assert chunked_event is not None
    assert chunked_event.at_ms == direct_event.at_ms
    assert chunked_event.freq_hz == direct_event.freq_hz
    assert chunked_event.duration_ms == direct_event.duration_ms


def test_voicemail_heuristic_7s_far_speech() -> None:
    sample_rate = 8000
    heuristic = VoicemailHeuristic()
    burst = synth_tone(440.0, 1000, sample_rate)

    for second in range(7):
        heuristic.feed_far(burst, sample_rate, float(second))

    assert heuristic.likely_machine


def test_voicemail_heuristic_short_far_speech() -> None:
    sample_rate = 8000
    heuristic = VoicemailHeuristic()

    heuristic.feed_far(synth_tone(440.0, 3000, sample_rate), sample_rate, 0.0)

    assert not heuristic.likely_machine


def test_far_speech_active_true_while_run_open_and_cleared_by_our_speech() -> None:
    sample_rate = 8000
    heuristic = VoicemailHeuristic()
    assert heuristic.far_speech_active is False

    heuristic.feed_far(synth_tone(440.0, 1000, sample_rate), sample_rate, 0.0)
    assert heuristic.far_speech_active is True

    heuristic.note_our_speech(1.0)
    assert heuristic.far_speech_active is False


def test_voicemail_heuristic_our_speech_breaks_greeting() -> None:
    sample_rate = 8000
    heuristic = VoicemailHeuristic()
    burst = synth_tone(440.0, 1000, sample_rate)

    for second in range(7):
        if second == 2:
            heuristic.note_our_speech(float(second))
        heuristic.feed_far(burst, sample_rate, float(second))

    assert not heuristic.likely_machine
