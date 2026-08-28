"""Pure unit tests over llm_client's message-shaping and response-parsing internals
(the review's BLOCKER 1 + SHOULD-FIX 8/9 + NIT (a)), plus one end-to-end MockTransport
test proving the OpenAI branch actually works through the SMS agent - it previously had
zero coverage.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.services import llm_client, sms_agent
from app.services.llm_client import ChatTurn, LLMError, ToolCall, chat
from tests.test_sms_agent import _inbound, _make_sms_org, _thread, _turns


# ======================================================================================
# _anthropic_messages: rules (a)/(b)/(c) from the review's BLOCKER 1
# ======================================================================================
def test_anthropic_messages_basic_user_assistant_round_trip():
    turns = [ChatTurn(role="user", content="hi"), ChatTurn(role="assistant", content="hello")]
    out = llm_client._anthropic_messages(turns)
    assert out == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def test_rule_a_drops_turns_with_empty_content_and_no_tool_calls():
    turns = [
        ChatTurn(role="user", content="hi"),
        ChatTurn(role="assistant", content=""),  # e.g. a media-only MMS with no text
        ChatTurn(role="user", content="still there?"),
    ]
    out = llm_client._anthropic_messages(turns)
    assert out == [
        {"role": "user", "content": "hi\nstill there?"},
    ]


def test_rule_a_keeps_an_empty_text_assistant_turn_that_has_tool_calls():
    turns = [
        ChatTurn(role="user", content="book me tomorrow"),
        ChatTurn(
            role="assistant",
            content="",
            tool_calls=(ToolCall(id="call-1", name="book_appointment", arguments={}),),
        ),
    ]
    out = llm_client._anthropic_messages(turns)
    assert out[-1]["role"] == "assistant"
    assert out[-1]["content"] == [
        {"type": "tool_use", "id": "call-1", "name": "book_appointment", "input": {}}
    ]


def test_rule_b_drops_leading_turns_until_first_user():
    """A thread whose oldest message is OUR outbound (org texted first) must not start
    the array with role "assistant" - Anthropic requires the array to start "user"."""
    turns = [
        ChatTurn(role="assistant", content="Hi, following up on your inquiry"),
        ChatTurn(role="user", content="who is this"),
    ]
    out = llm_client._anthropic_messages(turns)
    assert out == [{"role": "user", "content": "who is this"}]


def test_rule_b_raises_when_no_user_turn_survives():
    turns = [ChatTurn(role="assistant", content="Hello?"), ChatTurn(role="assistant", content="?")]
    with pytest.raises(LLMError, match="no usable history"):
        llm_client._anthropic_messages(turns)


def test_rule_c_merges_adjacent_same_role_plain_text_with_newline():
    """Two inbounds in a row (the contact sent two texts before anyone replied)."""
    turns = [
        ChatTurn(role="user", content="hey"),
        ChatTurn(role="user", content="you there?"),
        ChatTurn(role="assistant", content="yes!"),
    ]
    out = llm_client._anthropic_messages(turns)
    assert out == [
        {"role": "user", "content": "hey\nyou there?"},
        {"role": "assistant", "content": "yes!"},
    ]


def test_rule_c_merges_adjacent_tool_results_into_one_user_message():
    """Two tool calls in one assistant round produce two consecutive "tool" ChatTurns -
    Anthropic requires both tool_results to land in ONE user message, not two."""
    turns = [
        ChatTurn(role="user", content="book two things"),
        ChatTurn(
            role="assistant",
            content="",
            tool_calls=(
                ToolCall(id="call-1", name="book_appointment", arguments={"when": "mon"}),
                ToolCall(id="call-2", name="kb_search", arguments={"query": "pricing"}),
            ),
        ),
        ChatTurn(role="tool", tool_call_id="call-1", tool_name="book_appointment", content="ok"),
        ChatTurn(role="tool", tool_call_id="call-2", tool_name="kb_search", content="no hits"),
    ]
    out = llm_client._anthropic_messages(turns)
    tool_result_message = out[-1]
    assert tool_result_message["role"] == "user"
    assert tool_result_message["content"] == [
        {"type": "tool_result", "tool_use_id": "call-1", "content": "ok"},
        {"type": "tool_result", "tool_use_id": "call-2", "content": "no hits"},
    ]


def test_rule_c_merge_prefers_blocks_when_either_side_is_blocks():
    """A plain-text user turn immediately followed by a tool-result "user" turn (the
    model text-replied AND the harness fed a tool result back to back) must not silently
    stringify the tool_result block - it becomes a mixed block list."""
    turns = [
        ChatTurn(role="user", content="hi"),
        ChatTurn(role="tool", tool_call_id="call-1", tool_name="kb_search", content="hits"),
    ]
    # Force both onto "user" by hand-building the merge rather than through a realistic
    # ChatTurn sequence (the tool ChatTurn always maps to "user" already) - this exercises
    # _merge_anthropic_content directly for the plain-string + blocks combination.
    merged = llm_client._merge_anthropic_content("hi", [{"type": "text", "text": "hits"}])
    assert merged == [{"type": "text", "text": "hi"}, {"type": "text", "text": "hits"}]
    # And the realistic path (two "user"-role turns back to back) merges into one message,
    # with the plain "hi" text coerced into its own text block ahead of the tool_result.
    out = llm_client._anthropic_messages(turns)
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert out[0]["content"] == [
        {"type": "text", "text": "hi"},
        {"type": "tool_result", "tool_use_id": "call-1", "content": "hits"},
    ]


# ======================================================================================
# _openai_messages: tool_calls <-> tool-role round trip
# ======================================================================================
def test_openai_messages_system_prompt_prepended():
    out = llm_client._openai_messages("be nice", [ChatTurn(role="user", content="hi")])
    assert out[0] == {"role": "system", "content": "be nice"}


def test_openai_messages_tool_calls_round_trip():
    turns = [
        ChatTurn(role="user", content="book it"),
        ChatTurn(
            role="assistant",
            content="",
            tool_calls=(
                ToolCall(id="call-1", name="book_appointment", arguments={"when": "mon"}),
            ),
        ),
        ChatTurn(
            role="tool", tool_call_id="call-1", tool_name="book_appointment", content="booked"
        ),
    ]
    out = llm_client._openai_messages("", turns)
    assistant_msg = out[1]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["tool_calls"] == [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "book_appointment", "arguments": json.dumps({"when": "mon"})},
        }
    ]
    tool_msg = out[2]
    assert tool_msg == {"role": "tool", "tool_call_id": "call-1", "content": "booked"}


# ======================================================================================
# Parsers: tool_use <-> tool_result and tool_calls <-> tool-role, plus non-dict guards.
# ======================================================================================
def test_parse_anthropic_response_text_and_tool_use():
    data = {
        "content": [
            {"type": "text", "text": "Sure, "},
            {"type": "tool_use", "id": "call-1", "name": "kb_search", "input": {"query": "hi"}},
        ]
    }
    result = llm_client._parse_anthropic_response(data)
    assert result.text == "Sure, "
    expected_call = ToolCall(id="call-1", name="kb_search", arguments={"query": "hi"})
    assert result.tool_calls == (expected_call,)


def test_parse_anthropic_response_raises_on_empty_content():
    with pytest.raises(LLMError):
        llm_client._parse_anthropic_response({"content": []})


def test_parse_anthropic_response_raises_on_non_dict():
    with pytest.raises(LLMError, match="non-object body"):
        llm_client._parse_anthropic_response("not a dict")  # type: ignore[arg-type]


def test_parse_openai_response_text_and_tool_calls():
    data = {
        "choices": [
            {
                "message": {
                    "content": "Sure!",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {
                                "name": "kb_search",
                                "arguments": json.dumps({"query": "hi"}),
                            },
                        }
                    ],
                }
            }
        ]
    }
    result = llm_client._parse_openai_response(data)
    assert result.text == "Sure!"
    expected_call = ToolCall(id="call-1", name="kb_search", arguments={"query": "hi"})
    assert result.tool_calls == (expected_call,)


def test_parse_openai_response_raises_on_non_dict():
    with pytest.raises(LLMError, match="non-object body"):
        llm_client._parse_openai_response([1, 2, 3])  # type: ignore[arg-type]


def test_parse_openai_response_raises_on_empty_choices():
    with pytest.raises(LLMError):
        llm_client._parse_openai_response({"choices": []})


# ======================================================================================
# chat(): non-dict 200 body, and the OpenAI max_completion_tokens vs max_tokens split.
# ======================================================================================
async def test_chat_raises_on_non_dict_200_body():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(LLMError, match="non-object body"):
            await chat(
                client,
                provider="anthropic",
                model="claude-haiku-4-5",
                api_key="k",
                system="",
                turns=[ChatTurn(role="user", content="hi")],
                tools=[],
            )
    finally:
        await client.aclose()


async def test_openai_uses_max_completion_tokens_not_max_tokens():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await chat(
            client,
            provider="openai",
            model="gpt-5-class-thing",
            api_key="k",
            system="",
            turns=[ChatTurn(role="user", content="hi")],
            tools=[],
            max_tokens=256,
        )
    finally:
        await client.aclose()

    assert captured["payload"]["max_completion_tokens"] == 256
    assert "max_tokens" not in captured["payload"]


async def test_anthropic_still_uses_max_tokens():
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hi"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await chat(
            client,
            provider="anthropic",
            model="claude-haiku-4-5",
            api_key="k",
            system="",
            turns=[ChatTurn(role="user", content="hi")],
            tools=[],
            max_tokens=256,
        )
    finally:
        await client.aclose()

    assert captured["payload"]["max_tokens"] == 256
    assert "max_completion_tokens" not in captured["payload"]


# ======================================================================================
# End-to-end: the OpenAI provider branch through the actual SMS agent turn engine.
# Zero prior coverage of this path (the review's SHOULD-FIX 8).
# ======================================================================================
def _openai_client(bodies: list) -> httpx.AsyncClient:
    queue = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        if not queue:
            return httpx.Response(500, json={"error": {"message": "no scripted response left"}})
        return httpx.Response(200, json=queue.pop(0))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _openai_text_reply(text: str) -> dict:
    return {"choices": [{"message": {"content": text, "tool_calls": []}}]}


async def test_maybe_reply_with_openai_provider_end_to_end(
    app_with_carrier, session, monkeypatch
):
    """Driven through the REAL auto-trigger (webhook -> messaging._ingest_inbound ->
    sms_agent.spawn_from_ingest), same as test_sms_agent.py - that path now always uses
    the app's real settings, so app.state.settings needs an openai key before the LLM
    call can get past llm_client.chat()'s api_key check."""
    from pydantic import SecretStr

    client, fake, application = app_with_carrier
    token, org, h, _ = await _make_sms_org(
        client, "llm-openai@example.com", "Org LLM OpenAI", llm_provider="openai"
    )
    org_id = uuid.UUID(org["id"])
    application.state.settings.openai_api_key = SecretStr("test-openai-key")
    monkeypatch.setattr(
        sms_agent,
        "_default_http_client",
        lambda: _openai_client([_openai_text_reply("Yes, 9-5 on Saturdays!")]),
    )

    await _inbound(client, "Hi, are you open on weekends?", "llm-openai-1")
    await sms_agent.wait_for_pending_sms_tasks()

    assert fake.sent[-1].text == "Yes, 9-5 on Saturdays!"
    thread = await _thread(session, org_id)
    turns = await _turns(session, org_id, thread.id)
    assert turns[-1].status == "replied"
