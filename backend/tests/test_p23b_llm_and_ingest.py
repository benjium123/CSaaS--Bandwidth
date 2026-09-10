"""P23b tests for the new LLM vendors and safer URL ingest.

Covers DeepSeek/Groq using the OpenAI-compatible shape, Google Gemini's custom
request/response shape and malformed-response guards, plus URL ingest cap/SSRF
behaviour through the real async ORM session.
"""

from __future__ import annotations

import json
import socket

import httpx
import pytest
import sqlalchemy as sa

from app.models import KbChunk
from app.services import kb_ingest, llm_client
from app.services.llm_client import ChatTurn, LLMError, ToolCall, chat
from tests.test_p23a_kb import _add_org


async def test_deepseek_and_groq_use_the_openai_shape_with_the_right_token_parameter():
    """DeepSeek and Groq must use OpenAI message/tool shapes, but DeepSeek needs
    `max_tokens` while Groq and OpenAI need `max_completion_tokens`."""
    cases = [
        ("deepseek", "deepseek-chat", llm_client._DEEPSEEK_URL, "max_tokens"),
        ("groq", "llama-3.3-70b-versatile", llm_client._GROQ_URL, "max_completion_tokens"),
        ("openai", "gpt-4o-mini", llm_client._OPENAI_URL, "max_completion_tokens"),
    ]

    for provider, model, expected_url, expected_token_param in cases:
        captured: dict = {}

        # Bind `captured` as a default argument: the closure is redefined every loop
        # iteration and a late-bound reference would record into the LAST dict for all.
        async def handler(request: httpx.Request, captured=captured) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            captured["payload"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await chat(
                client,
                provider=provider,
                model=model,
                api_key="key",
                system="sys",
                turns=[ChatTurn(role="user", content="hi")],
                tools=[],
                max_tokens=256,
            )

        assert captured["url"] == expected_url
        assert captured["auth"] == "Bearer key"
        assert captured["payload"]["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        if expected_token_param == "max_tokens":
            assert captured["payload"]["max_tokens"] == 256
            assert "max_completion_tokens" not in captured["payload"]
        else:
            assert captured["payload"]["max_completion_tokens"] == 256
            assert "max_tokens" not in captured["payload"]


async def test_gemini_request_and_response_shape():
    """Google Gemini uses its own generateContent shape with x-goog-api-key auth,
    user/model contents, and usageMetadata token counts."""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "Hello"}, {"text": " there"}]}}
                ],
                "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 34},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await chat(
            client,
            provider="google",
            model="gemini-1.5-flash",
            api_key="key",
            system="Be brief",
            turns=[
                ChatTurn(role="user", content="hi"),
                ChatTurn(role="assistant", content="hello"),
            ],
            tools=[],
            max_tokens=256,
        )

    assert captured["url"] == llm_client._GOOGLE_URL_TEMPLATE.format(
        model="gemini-1.5-flash"
    )
    assert captured["headers"]["x-goog-api-key"] == "key"
    assert captured["payload"]["contents"] == [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "hello"}]},
    ]
    assert captured["payload"]["generationConfig"] == {"maxOutputTokens": 256}
    assert captured["payload"]["systemInstruction"] == {"parts": [{"text": "Be brief"}]}
    assert result.text == "Hello there"
    assert result.tokens_in == 12
    assert result.tokens_out == 34


def test_gemini_tool_call_is_parsed():
    """Gemini functionCall parts become ToolCall objects; the id is empty on purpose."""
    result = llm_client._parse_gemini_response(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "kb_search",
                                    "args": {"query": "pricing"},
                                }
                            }
                        ]
                    }
                }
            ]
        }
    )
    assert result.text == ""
    assert result.tool_calls == (
        ToolCall(id="", name="kb_search", arguments={"query": "pricing"}),
    )


def test_gemini_empty_candidates_is_an_llm_error():
    """An empty candidates array must raise LLMError, never return an empty result."""
    with pytest.raises(LLMError, match="no completion"):
        llm_client._parse_gemini_response({"candidates": []})


def test_gemini_malformed_body_raises_llm_error_not_keyerror():
    """Missing Gemini content/parts must surface as LLMError rather than KeyError."""
    with pytest.raises(LLMError):
        llm_client._parse_gemini_response({"candidates": [{"content": {"parts": []}}]})
    with pytest.raises(LLMError):
        llm_client._parse_gemini_response({"candidates": [{"no": "content"}]})


def _patch_dns(monkeypatch, ip: str) -> None:
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr("app.services.webhooks_out.socket.getaddrinfo", fake_getaddrinfo)


async def test_url_ingest_stops_reading_past_the_cap(session, monkeypatch):
    """A response larger than MAX_URL_BYTES is stored failed with no chunks instead of
    buffering the whole body."""
    org = await _add_org(session, "URL Ingest Cap")
    _patch_dns(monkeypatch, "93.184.216.34")

    cap = kb_ingest.MAX_URL_BYTES
    body = b"<html><body>" + b"a" * cap + b"</body></html>"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        doc = await kb_ingest.ingest_url(
            session, org.id, title="big", url="https://example.com/big", client=client
        )
    finally:
        await client.aclose()

    assert doc.status == "failed"
    assert doc.chunk_count == 0
    assert doc._ingest_detail == "That page is too big."

    chunks = (
        await session.execute(
            sa.select(KbChunk).where(KbChunk.document_id == doc.id)
        )
    ).scalars().all()
    assert chunks == []


async def test_url_ingest_refuses_a_host_that_resolves_privately(session, monkeypatch):
    """The outbound-webhook resolver closes the public-name/private-A-record hole and
    stores the failure rather than raising."""
    org = await _add_org(session, "URL Ingest Private")
    _patch_dns(monkeypatch, "127.0.0.1")

    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        raise AssertionError("fetch should not be attempted")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        doc = await kb_ingest.ingest_url(
            session,
            org.id,
            title="private",
            url="https://public-looking.example/page",
            client=client,
        )
    finally:
        await client.aclose()

    assert not called
    assert doc.status == "failed"
    assert doc._ingest_detail == "That web address cannot be reached from here."


async def test_url_ingest_still_indexes_a_normal_page(session, monkeypatch):
    """The streaming happy path still decodes text/html and indexes chunks."""
    org = await _add_org(session, "URL Ingest Normal")
    _patch_dns(monkeypatch, "93.184.216.34")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<html><body><p>Hello world</p></body></html>",
            headers={"content-type": "text/html"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        doc = await kb_ingest.ingest_url(
            session, org.id, title="normal", url="https://example.com/normal", client=client
        )
    finally:
        await client.aclose()

    assert doc.status == "indexed"
    assert doc.chunk_count > 0
