"""Pure-logic test for the warm-handoff summary: format_handoff_summary() renders the
live call's `deque(maxlen=6)` of (role, text) transcript entries into the "role: text"
per-line string sent as the `summary` field of POST /api/v1/agent/handoff.

Lives in backend_client.py (not ai_agent.py, which imports livekit-agents at module
scope) specifically so this is runnable under the plain backend venv without the SDK -
same reasoning as process_metric in ai_agent.py, but that one still needs real
livekit.agents.metrics dataclasses to construct inputs and so stays SDK-only.
"""

from __future__ import annotations

from collections import deque

from agents.backend_client import format_handoff_summary


def test_empty_transcript_tail_is_an_empty_summary() -> None:
    assert format_handoff_summary(deque(maxlen=6)) == ""


def test_roles_are_labeled_and_lines_are_in_chronological_order() -> None:
    tail: deque[tuple[str, str]] = deque(maxlen=6)
    tail.append(("user", "I need to reschedule"))
    tail.append(("agent", "Sure, what day works?"))
    tail.append(("user", "Thursday afternoon"))

    assert format_handoff_summary(tail) == (
        "user: I need to reschedule\n"
        "agent: Sure, what day works?\n"
        "user: Thursday afternoon"
    )


def test_deque_maxlen_six_keeps_only_the_last_six_entries() -> None:
    tail: deque[tuple[str, str]] = deque(maxlen=6)
    for i in range(10):
        role = "user" if i % 2 == 0 else "agent"
        tail.append((role, f"turn-{i}"))

    # Entries 0-3 must have fallen off the deque itself (maxlen=6), not just been
    # skipped by the formatter - format_handoff_summary must not re-truncate on top of
    # whatever the deque already holds, so this also asserts it does no extra slicing.
    assert format_handoff_summary(tail) == (
        "user: turn-4\n"
        "agent: turn-5\n"
        "user: turn-6\n"
        "agent: turn-7\n"
        "user: turn-8\n"
        "agent: turn-9"
    )


def test_works_with_any_iterable_not_just_a_deque() -> None:
    assert format_handoff_summary([("agent", "hello")]) == "agent: hello"
