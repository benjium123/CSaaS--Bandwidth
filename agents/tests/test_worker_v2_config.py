"""AI agents v2: config fetch with fallback, limits, disclosure order, LLM choice, usage and
outcome posts. Pure unit tests - no livekit import."""

from __future__ import annotations

import pytest

from agents.backend_client import BackendClient
from agents.worker_config import (
    billable_voice_seconds,
    is_credit_refusal,
    opening_lines,
    resolve_limits,
    resolve_llm,
)

pytestmark = pytest.mark.asyncio


class _Resp:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class _Client:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict | None]] = []

    async def _next(self, method, url, json=None):
        self.calls.append((method, url, json))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def get(self, url, headers=None, params=None):
        return await self._next("GET", url)

    async def post(self, url, headers=None, json=None):
        return await self._next("POST", url, json)


def _backend(client) -> BackendClient:
    return BackendClient("http://b", "k", "s" * 40, client=client)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_):
        return None

    monkeypatch.setattr("agents.backend_client.asyncio.sleep", _instant)


async def test_fetch_config_ok():
    client = _Client([_Resp(200, {"greeting": "hi"})])
    assert await _backend(client).fetch_config("c1") == {"greeting": "hi"}
    assert client.calls[0][1].endswith("/api/v1/agent/config/c1")


async def test_fetch_config_402_is_a_credit_refusal():
    cfg = await _backend(_Client([_Resp(402)])).fetch_config("c1")
    assert is_credit_refusal(cfg)


async def test_fetch_config_network_failure_returns_none_so_caller_falls_back():
    assert await _backend(_Client([RuntimeError("down")])).fetch_config("c1") is None
    assert is_credit_refusal(None) is False


async def test_fetch_config_404_returns_none_without_retry():
    client = _Client([_Resp(404)])
    assert await _backend(client).fetch_config("c1") is None
    assert len(client.calls) == 1


def test_limits_come_from_config_with_env_fallbacks():
    assert resolve_limits({"max_call_seconds": 300, "silence_timeout_seconds": 7}, 900, 20) == (300, 7)
    assert resolve_limits({}, 900, 20) == (900, 20)
    assert resolve_limits({"max_call_seconds": 0, "silence_timeout_seconds": "x"}, 900, 20) == (900, 20)


def test_disclosure_is_spoken_before_the_greeting():
    cfg = {"ai_disclosure": True, "disclosure_text": "Automated.", "greeting": "Hello!"}
    assert opening_lines(cfg) == ["Automated.", "Hello!"]
    assert opening_lines({**cfg, "ai_disclosure": False}) == ["Hello!"]
    assert opening_lines({}) == []


def test_llm_choice_and_fallbacks():
    env = {"DEEPSEEK_API_KEY": "k", "TELNYX_API_KEY": "t"}
    ds = resolve_llm({"llm_provider": "deepseek"}, env)
    assert (ds["kind"], ds["base_url"], ds["model"]) == ("compat", "https://api.deepseek.com", "deepseek-chat")
    custom = resolve_llm({"llm_provider": "deepseek", "llm_base_url": "http://x/v1"}, env)
    assert custom["base_url"] == "http://x/v1"
    tx = resolve_llm({"llm_provider": "telnyx"}, env)
    assert (tx["base_url"], tx["model"]) == ("https://api.telnyx.com/v2/ai", "moonshotai/Kimi-K2-Instruct")
    assert resolve_llm({"llm_provider": "telnyx"}, {})["kind"] == "anthropic"
    assert resolve_llm({"llm_provider": "deepseek"}, {})["kind"] == "anthropic"
    assert resolve_llm({"llm_provider": "openai"}, {})["kind"] == "openai"
    assert resolve_llm({}, {})["kind"] == "anthropic"


def test_voice_seconds_round_up_and_never_negative():
    assert billable_voice_seconds(100.0, 160.2) == 61
    assert billable_voice_seconds(100.0, 160.0) == 60
    assert billable_voice_seconds(100.0, 90.0) == 0


async def test_usage_event_shape_and_retry_then_success():
    client = _Client([_Resp(500), _Resp(202)])
    assert await _backend(client).post_usage("c1", 61) is True
    assert len(client.calls) == 2
    event = client.calls[-1][2]["events"][0]
    assert event == {
        "call_id": "c1", "provider": "livekit", "kind": "voice", "metric": "ai_voice_seconds",
        "quantity": 61, "source": "worker", "idempotency_key": "ai-voice-c1",
    }


async def test_outcome_shape_and_never_raises():
    client = _Client([_Resp(200)])
    assert await _backend(client).post_outcome("c1") is True
    item = client.calls[0][2]["outcomes"][0]
    assert item == {"call_id": "c1", "disposition": "answered", "summary": "", "extracted": {}}
    assert await _backend(_Client([RuntimeError("x")])).post_outcome("c1") is False
