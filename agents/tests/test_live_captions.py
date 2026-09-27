"""Tests for the pure helpers and Router in agents/live_captions.py.

These tests deliberately do not import livekit; the module keeps livekit imports
lazy so the helpers are importable in any agents test environment.
"""

from __future__ import annotations

import hashlib
import io
import json
import struct
import wave

import httpx
import pytest

from agents.live_captions import (
    Router,
    caption_payload,
    stt_token,
    utterance_wav,
)


class FakeFrame:
    def __init__(self, data: bytes, sample_rate: int = 16000, num_channels: int = 1):
        self.data = data
        self.sample_rate = sample_rate
        self.num_channels = num_channels


class FakeClock:
    def __init__(self, start: float = 100.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_stt_token_deterministic() -> None:
    expected = hashlib.sha256(b"stt:secret").hexdigest()
    assert stt_token("secret") == expected
    assert stt_token("secret") == stt_token("secret")
    assert stt_token("secret") != stt_token("other")


def test_utterance_wav_16k_mono_fake_valid() -> None:
    sample_count = 160
    samples = [i % 30000 for i in range(sample_count)]
    data = struct.pack(f"<{sample_count}h", *samples)

    wav = utterance_wav([FakeFrame(data)])

    with wave.open(io.BytesIO(wav), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16000
        assert wav_file.getnframes() == sample_count
        assert wav_file.readframes(sample_count) == data


def test_caption_payload_json_shape() -> None:
    payload = caption_payload("user", "hello world", 1234, "local")
    assert json.loads(payload.decode()) == {
        "type": "caption",
        "role": "user",
        "text": "hello world",
        "at_ms": 1234,
        "engine": "local",
    }


@pytest.mark.asyncio
async def test_router_local_200_returns_local_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"load1": 0.1, "cpus": 2, "live_busy": 0, "live_capacity": 10},
            )
        if request.url.path == "/transcribe":
            return httpx.Response(200, json={"segments": [{"text": "hello"}]})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = Router(
            client=client,
            stt_url="http://stt.test",
            api_secret="secret",
            overflow="none",
            deepgram_api_key="",
            now=lambda: 100.0,
        )
        result = await router.transcribe(b"wav", 1.0)

    assert result == ("hello", "local")


@pytest.mark.asyncio
async def test_router_local_503_overflows_to_deepgram_and_counts_seconds() -> None:
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"load1": 0.1, "cpus": 2, "live_busy": 0, "live_capacity": 10},
            )
        if request.url.path == "/transcribe":
            return httpx.Response(503)
        if request.url.host == "api.deepgram.com":
            return httpx.Response(
                200,
                json={
                    "results": {
                        "channels": [
                            {"alternatives": [{"transcript": "deep hello"}]}
                        ]
                    }
                },
            )
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = Router(
            client=client,
            stt_url="http://stt.test",
            api_secret="secret",
            overflow="deepgram",
            deepgram_api_key="dgkey",
            now=clock,
        )
        result = await router.transcribe(b"wav", 2.5)

    assert result == ("deep hello", "deepgram")
    assert router.deepgram_seconds == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_router_overflow_none_local_failure_returns_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"load1": 0.1, "cpus": 2, "live_busy": 0, "live_capacity": 10},
            )
        if request.url.path == "/transcribe":
            return httpx.Response(503)
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = Router(
            client=client,
            stt_url="http://stt.test",
            api_secret="secret",
            overflow="none",
            deepgram_api_key="",
            now=lambda: 100.0,
        )
        result = await router.transcribe(b"wav", 1.0)

    assert result == ("", None)


@pytest.mark.asyncio
async def test_router_health_busy_skips_local_entirely() -> None:
    transcribe_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal transcribe_called
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"load1": 2.0, "cpus": 2, "live_busy": 0, "live_capacity": 10},
            )
        if request.url.path == "/transcribe":
            transcribe_called = True
            return httpx.Response(200, json={"segments": [{"text": "should not"}]})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = Router(
            client=client,
            stt_url="http://stt.test",
            api_secret="secret",
            overflow="none",
            deepgram_api_key="",
            now=lambda: 100.0,
        )
        result = await router.transcribe(b"wav", 1.0)

    assert result == ("", None)
    assert transcribe_called is False


@pytest.mark.asyncio
async def test_router_health_cached_within_five_seconds() -> None:
    clock = FakeClock()
    health_requests = 0
    transcribe_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal health_requests, transcribe_requests
        if request.url.path == "/health":
            health_requests += 1
            return httpx.Response(
                200,
                json={"load1": 0.1, "cpus": 2, "live_busy": 0, "live_capacity": 10},
            )
        if request.url.path == "/transcribe":
            transcribe_requests += 1
            return httpx.Response(200, json={"segments": [{"text": "ok"}]})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = Router(
            client=client,
            stt_url="http://stt.test",
            api_secret="secret",
            overflow="none",
            deepgram_api_key="",
            now=clock,
        )
        first = await router.transcribe(b"wav", 1.0)
        clock.advance(2.0)
        second = await router.transcribe(b"wav", 1.0)

    assert first == ("ok", "local")
    assert second == ("ok", "local")
    assert health_requests == 1
    assert transcribe_requests == 2
