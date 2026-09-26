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
