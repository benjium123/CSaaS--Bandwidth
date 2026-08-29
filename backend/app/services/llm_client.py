"""Thin async chat-completion client for the SMS agent turn engine.

Plan DR-6 keeps this module free of vendor SDKs; only httpx is used.
The caller owns the httpx.AsyncClient so tests can inject a transport.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o-mini",
}

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class LLMError(RuntimeError):
    """Provider refused or was unreachable; message carries the provider's own text."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ChatTurn:
    role: str
    content: str = ""
    tool_calls: tuple = field(default=())
    tool_call_id: str = ""
    tool_name: str = ""


@dataclass(frozen=True)
class ChatResult:
    text: str
    tool_calls: tuple
    # P13 DR-9: usage from the provider response; 0 when the provider omitted it.
    tokens_in: int = 0
    tokens_out: int = 0


def _is_blank_content(content: Any) -> bool:
    if isinstance(content, list):
        return len(content) == 0
    return not content


def _merge_anthropic_content(first: Any, second: Any) -> Any:
    """Join two message contents collapsing into one turn (adjacent same-role merge,
    rule (c)). Two plain strings join with a newline; once EITHER side is already a
    content-block list (a tool_use/tool_result/text block), both are coerced to blocks
    and concatenated - Anthropic has no mixed plain-text-plus-blocks representation."""
    first_is_blocks = isinstance(first, list)
    second_is_blocks = isinstance(second, list)
    if not first_is_blocks and not second_is_blocks:
        return f"{first}\n{second}"
    first_blocks = first if first_is_blocks else [{"type": "text", "text": first}]
    second_blocks = second if second_is_blocks else [{"type": "text", "text": second}]
    return [*first_blocks, *second_blocks]


def _normalize_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic's hard requirements a dumb per-ChatTurn mapping cannot guarantee on its
    own, enforced here once rather than pushed onto `_load_history` (which stays a plain
    "map every message in arrival order"):

      (a) a turn with no content and no tool_calls/tool_result is dropped - an empty text
          block is not a message Anthropic accepts;
      (b) the array must start with role "user" - any leading non-user turns (e.g. a
          thread where the org's own outbound message is the oldest thing in history) are
          dropped; if nothing survives, this history is unusable;
      (c) two adjacent messages of the SAME role are merged into one - real threads can
          have two inbounds (or two tool results) in a row, and Anthropic rejects
          consecutive same-role messages.
    """
    non_empty = [m for m in messages if not _is_blank_content(m.get("content"))]

    start = 0
    while start < len(non_empty) and non_empty[start]["role"] != "user":
        start += 1
    trimmed = non_empty[start:]
    if not trimmed:
        raise LLMError("no usable history")

    merged: list[dict[str, Any]] = [dict(trimmed[0])]
    for msg in trimmed[1:]:
        if merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] = _merge_anthropic_content(merged[-1]["content"], msg["content"])
        else:
            merged.append(dict(msg))
    return merged


def _anthropic_messages(turns: list[ChatTurn]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for turn in turns:
        if turn.role == "user":
            messages.append({"role": "user", "content": turn.content})
        elif turn.role == "assistant":
            if turn.tool_calls:
                content: list[dict[str, Any]] = []
                if turn.content:
                    content.append({"type": "text", "text": turn.content})
                for call in turn.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.arguments,
                        }
                    )
                messages.append({"role": "assistant", "content": content})
            else:
                messages.append({"role": "assistant", "content": turn.content})
        elif turn.role == "tool":
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": turn.tool_call_id,
                            "content": turn.content,
                        }
                    ],
                }
            )
        else:
            messages.append({"role": turn.role, "content": turn.content})
    return _normalize_anthropic_messages(messages)


def _openai_messages(system: str, turns: list[ChatTurn]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    for turn in turns:
        if turn.role == "user":
            messages.append({"role": "user", "content": turn.content})
        elif turn.role == "assistant":
            if turn.tool_calls:
                tool_calls = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in turn.tool_calls
                ]
                messages.append(
                    {
                        "role": "assistant",
                        "content": turn.content or None,
                        "tool_calls": tool_calls,
                    }
                )
            else:
                messages.append({"role": "assistant", "content": turn.content})
        elif turn.role == "tool":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": turn.tool_call_id,
                    "content": turn.content,
                }
            )
        else:
            messages.append({"role": turn.role, "content": turn.content})
    return messages


def _parse_anthropic_response(data: dict[str, Any]) -> ChatResult:
    if not isinstance(data, dict):
        raise LLMError("provider returned a non-object body")
    blocks = data.get("content")
    if not isinstance(blocks, list) or not blocks:
        raise LLMError("provider returned no completion")

    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text") or "")
        elif block.get("type") == "tool_use":
            arguments = block.get("input") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            tool_calls.append(
                ToolCall(
                    id=block.get("id") or "",
                    name=block.get("name") or "",
                    arguments=arguments,
                )
            )

    text = "".join(text_parts)
    if not text and not tool_calls:
        raise LLMError("provider returned no completion")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return ChatResult(
        text=text,
        tool_calls=tuple(tool_calls),
        tokens_in=int(usage.get("input_tokens") or 0),
        tokens_out=int(usage.get("output_tokens") or 0),
    )


def _parse_openai_response(data: dict[str, Any]) -> ChatResult:
    if not isinstance(data, dict):
        raise LLMError("provider returned a non-object body")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError("provider returned no completion")

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        message = {}

    text = message.get("content") or ""
    tool_calls: list[ToolCall] = []

    raw_tool_calls = message.get("tool_calls") or []
    if isinstance(raw_tool_calls, list):
        for item in raw_tool_calls:
            if not isinstance(item, dict):
                continue
            function = item.get("function")
            if not isinstance(function, dict):
                function = {}
            raw_arguments = function.get("arguments") or ""
            if isinstance(raw_arguments, dict):
                arguments = raw_arguments
            elif raw_arguments:
                try:
                    arguments = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    arguments = {}
            else:
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            tool_calls.append(
                ToolCall(
                    id=item.get("id") or "",
                    name=function.get("name") or "",
                    arguments=arguments,
                )
            )

    if not text and not tool_calls:
        raise LLMError("provider returned no completion")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return ChatResult(
        text=text,
        tool_calls=tuple(tool_calls),
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
    )


async def chat(
    client,
    *,
    provider: str,
    model: str,
    api_key: str,
    system: str,
    turns: list[ChatTurn],
    tools: list[ToolSpec],
    max_tokens: int = 1024,
    timeout: float = 30.0,
) -> ChatResult:
    if provider not in _DEFAULT_MODELS:
        raise LLMError(f"Unknown LLM provider: {provider}")
    if not api_key:
        raise LLMError(f"Missing API key for {provider}")

    resolved_model = model or _DEFAULT_MODELS[provider]

    if provider == "anthropic":
        url = _ANTHROPIC_URL
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": resolved_model,
            "max_tokens": max_tokens,
            "messages": _anthropic_messages(turns),
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in tools
            ]
    else:
        url = _OPENAI_URL
        headers = {
            "Authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        }
        payload = {
            "model": resolved_model,
            # NOT max_tokens: Chat Completions rejects it outright on o-series/gpt-5-class
            # models ("Unsupported parameter"), and `llm_model` is operator free text we
            # cannot sniff in advance. max_completion_tokens is accepted by every model
            # currently served on this endpoint, including the older ones.
            "max_completion_tokens": max_tokens,
            "messages": _openai_messages(system, turns),
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]

    try:
        response = await client.post(url, headers=headers, json=payload, timeout=timeout)
    except httpx.RequestError as exc:
        raise LLMError(f"Could not reach {provider}: {exc}") from exc

    if response.status_code != 200:
        detail = ""
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            detail = error.get("message") or ""
        if not detail:
            detail = response.text
        raise LLMError(f"{provider} error {response.status_code}: {detail[:200]}")

    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        raise LLMError(f"Could not reach {provider}: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("provider returned a non-object body")

    if provider == "anthropic":
        return _parse_anthropic_response(data)
    return _parse_openai_response(data)
