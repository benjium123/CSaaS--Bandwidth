"""Worker settings that the backend and the worker must AGREE on. Pure Python on purpose:
importing agents.ai_agent pulls in the whole livekit SDK, so anything a test in the plain
backend venv needs to check lives here instead."""

import os
from collections.abc import Mapping

#: Same literal as the backend's services/assistant_dispatch.py AI_AGENT_NAME_DEFAULT. The two
#: packages cannot import each other, so this is pinned by test and by name.
AGENT_NAME_DEFAULT = "ai-agent"


def resolve_agent_name(environ: Mapping[str, str] | None = None) -> str:
    """AI_AGENT_NAME wins when set and non-blank; otherwise the shared default."""
    env = os.environ if environ is None else environ
    name = env.get("AI_AGENT_NAME", AGENT_NAME_DEFAULT).strip()
    return name or AGENT_NAME_DEFAULT


#: P43: the silent call-monitor listener. Same literal as the backend's
#: services/monitor_calls.py MONITOR_AGENT_NAME_DEFAULT.
MONITOR_AGENT_NAME_DEFAULT = "call-monitor"


def resolve_monitor_agent_name(environ: Mapping[str, str] | None = None) -> str:
    """MONITOR_AGENT_NAME wins when set and non-blank; otherwise the shared default."""
    env = os.environ if environ is None else environ
    name = env.get("MONITOR_AGENT_NAME", MONITOR_AGENT_NAME_DEFAULT).strip()
    return name or MONITOR_AGENT_NAME_DEFAULT


#: Warm worker processes each worker keeps ready, so a call starts without the 2-5 s it takes
#: to spawn one. livekit-agents defaults to min(CPUs, 4) - four ~350 MB processes each, idle
#: nearly all day. One spare still answers instantly; the pool refills after each job starts.
IDLE_PROCESSES_DEFAULT = {"ai-agent": 2, "call-monitor": 1}


def resolve_idle_processes(worker: str, environ: Mapping[str, str] | None = None) -> int:
    """WORKER_IDLE_PROCESSES wins when it is a whole number >= 1 (never 0: that would make
    every call wait for a cold start); otherwise this worker's default."""
    env = os.environ if environ is None else environ
    default = IDLE_PROCESSES_DEFAULT.get(worker, 1)
    try:
        value = int(env.get("WORKER_IDLE_PROCESSES", "").strip())
    except ValueError:
        return default
    return value if value >= 1 else default


def role_for_participant(*, kind: object, attributes: Mapping[str, str], sip_kind: object) -> str:
    """Transcript role for a room participant: the phone side (a SIP participant, or anyone
    carrying a SIP call id) is "user"; the business's people in the browser are "agent"."""
    if kind == sip_kind or (attributes or {}).get("sip.callID"):
        return "user"
    return "agent"


def sip_call_active(attributes: Mapping[str, str]) -> bool:
    """Has the phone side of a SIP participant actually answered? livekit-sip reports
    ``sip.callStatus`` (dialing / ringing / active / hangup). Older bridges that don't
    report it only add the participant once the call is connected, so a missing status
    counts as answered."""
    status = (attributes or {}).get("sip.callStatus")
    return status is None or status == "active"


# --- AI agents v2: the worker's setup comes from GET /agent/config -------------------------
import math

#: Neutral disposition posted at call end when nothing better is known; a member of the
#: backend's OUTCOME_DISPOSITIONS.
DEFAULT_OUTCOME_DISPOSITION = "answered"

#: OpenAI-compatible providers the worker can drive through livekit's openai plugin:
#: provider -> (default base_url, API key env var, default model, base_url env override).
OPENAI_COMPATIBLE_LLMS = {
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek-chat", "DEEPSEEK_BASE_URL"),
    "telnyx": (
        "https://api.telnyx.com/v2/ai",
        "TELNYX_API_KEY",
        "moonshotai/Kimi-K2-Instruct",
        "TELNYX_AI_BASE_URL",
    ),
}


def positive_int(value: object, fallback: int) -> int:
    """`value` as an int >= 1, else `fallback` (profile limits come from JSON/DB)."""
    try:
        number = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return fallback
    return number if number >= 1 else fallback


def resolve_limits(config: Mapping[str, object], max_fallback: int, silence_fallback: int) -> tuple[int, int]:
    """(max_call_seconds, silence_timeout_seconds): the config's values win; the env
    constants are fallbacks only."""
    return (
        positive_int(config.get("max_call_seconds"), max_fallback),
        positive_int(config.get("silence_timeout_seconds"), silence_fallback),
    )


def is_credit_refusal(config: Mapping[str, object] | None) -> bool:
    return bool(config and config.get("_refused"))


def opening_lines(config: Mapping[str, object]) -> list[str]:
    """What the AI says first: the disclosure (when on), then the greeting."""
    lines: list[str] = []
    if config.get("ai_disclosure"):
        text = str(config.get("disclosure_text") or "").strip()
        if text:
            lines.append(text)
    greeting = str(config.get("greeting") or "").strip()
    if greeting:
        lines.append(greeting)
    return lines


def resolve_llm(config: Mapping[str, object], environ: Mapping[str, str] | None = None) -> dict:
    """Pure LLM choice. Returns {"kind": "anthropic"|"openai"|"compat", "model", "base_url",
    "api_key"}. A compat provider (deepseek, telnyx) with no API key falls back to
    anthropic, as deepseek always has."""
    env = os.environ if environ is None else environ
    provider = str(config.get("llm_provider") or env.get("LLM_PROVIDER") or "anthropic").strip().lower()
    model = str(config.get("llm_model") or "")
    if provider == "openai":
        return {"kind": "openai", "model": model or "gpt-4o-mini", "base_url": "", "api_key": ""}
    if provider in OPENAI_COMPATIBLE_LLMS:
        default_url, key_env, default_model, url_env = OPENAI_COMPATIBLE_LLMS[provider]
        api_key = env.get(key_env, "").strip()
        if api_key:
            base_url = str(config.get("llm_base_url") or "").strip() or env.get(url_env, "").strip() or default_url
            return {"kind": "compat", "model": model or default_model, "base_url": base_url, "api_key": api_key}
    return {"kind": "anthropic", "model": "" if provider != "anthropic" else model, "base_url": "", "api_key": ""}


def billable_voice_seconds(joined_at: float, left_at: float) -> int:
    """Whole seconds from the AI joining to leaving the room, rounded up (never negative)."""
    return max(0, math.ceil(left_at - joined_at))
