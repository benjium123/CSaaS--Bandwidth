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
