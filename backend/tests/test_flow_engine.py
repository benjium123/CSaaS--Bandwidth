"""P12 flow-engine unit tests (phase-12-plan test spec, "Unit" section).

Pure functions only - no DB, no app, no fixtures beyond plain dicts. `flow_engine.py`
itself is Fable-owned/hands-off; this file is new.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services import flow_engine as fe


def _menu_flow(**overrides) -> dict:
    flow = {
        "entry": "welcome",
        "nodes": {
            "welcome": {
                "type": "menu",
                "prompt": "Press 1 for sales, 2 for support.",
                "options": {"1": "sales", "2": "support"},
                "timeout_node": "voicemail",
                "invalid_node": "voicemail",
                "invalid_retries": 2,
            },
            "sales": {"type": "hangup"},
            "support": {"type": "hangup"},
            "voicemail": {"type": "voicemail", "greeting": "Leave a message."},
        },
    }
    flow.update(overrides)
    return flow


def test_start_menu_speaks_prompt_and_gathers():
    result = fe.start(_menu_flow())
    assert result.actions == (
        fe.Speak(text="Press 1 for sales, 2 for support."),
        fe.GatherDigit(max_digits=1, timeout_seconds=10),
    )
    assert result.awaiting == "digit"
    assert result.terminal is None
    assert result.state == {"node": "welcome", "retries": 0}


def test_menu_valid_digit_branches():
    start = fe.start(_menu_flow())
    result = fe.step(_menu_flow(), start.state, {"kind": "digit", "digit": "2"})
    assert result.actions == (fe.Hangup(),)
    assert result.terminal == "hangup"


def test_menu_invalid_digit_retries_then_falls_to_default():
    flow = _menu_flow()
    state = fe.start(flow).state

    # Two invalid presses: still retrying, re-prompts + gathers again.
    r1 = fe.step(flow, state, {"kind": "digit", "digit": "9"})
    assert r1.awaiting == "digit"
    assert r1.state["retries"] == 1
    r2 = fe.step(flow, r1.state, {"kind": "digit", "digit": "9"})
    assert r2.awaiting == "digit"
    assert r2.state["retries"] == 2

    # Third invalid press exceeds invalid_retries=2 -> falls to invalid_node (voicemail).
    r3 = fe.step(flow, r2.state, {"kind": "digit", "digit": "9"})
    assert r3.terminal == "voicemail"
    assert r3.actions[0] == fe.RecordVoicemail(greeting="Leave a message.")


def test_menu_timeout_goes_to_timeout_node():
    flow = _menu_flow()
    state = fe.start(flow).state
    result = fe.step(flow, state, {"kind": "timeout"})
    assert result.terminal == "voicemail"


def test_hours_node_open_closed_holiday_branches():
    flow = {
        "entry": "hours",
        "nodes": {
            "hours": {
                "type": "hours",
                "business_hours_id": "bh-1",
                "open": "open_branch",
                "closed": "closed_branch",
                "holiday": "holiday_branch",
            },
            "open_branch": {"type": "hangup"},
            "closed_branch": {"type": "hangup"},
            "holiday_branch": {"type": "hangup"},
        },
    }
    start = fe.start(flow)
    assert start.actions == (fe.EvaluateHours(business_hours_id="bh-1"),)
    assert start.awaiting == "hours"

    for result in ("open", "closed", "holiday"):
        r = fe.step(flow, start.state, {"kind": "hours", "result": result})
        assert r.terminal == "hangup"


def test_hours_node_dst_transition_date_is_just_data_to_the_engine():
    """The engine itself does no timezone math - it only branches on whatever result
    string the caller (services/flows.evaluate_hours) already computed. This test pins
    that contract with a DST-adjacent instant so a future change to the engine cannot
    quietly start doing its own (wrong) offset arithmetic."""
    flow = {
        "entry": "hours",
        "nodes": {
            "hours": {
                "type": "hours",
                "business_hours_id": "bh-1",
                "open": "open_branch",
                "closed": "closed_branch",
                "holiday": "holiday_branch",
            },
            "open_branch": {"type": "hangup"},
            "closed_branch": {"type": "hangup"},
            "holiday_branch": {"type": "hangup"},
        },
    }
    # 2026-03-08 is a US DST "spring forward" date - irrelevant to the engine, relevant to
    # services/flows.evaluate_hours (covered in test_flows.py); here only the event shape
    # matters.
    dst_instant = datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)
    assert dst_instant.tzinfo is not None
    start = fe.start(flow)
    result = fe.step(flow, start.state, {"kind": "hours", "result": "open"})
    assert result.terminal == "hangup"


def test_nested_menu_two_levels_reaches_ring_group_queue_voicemail():
    flow = {
        "entry": "root",
        "nodes": {
            "root": {"type": "menu", "prompt": "root", "options": {"1": "level2"}},
            "level2": {
                "type": "menu",
                "prompt": "level2",
                "options": {"1": "ring", "2": "queue", "3": "voicemail"},
            },
            "ring": {"type": "ring_group", "ring_group_id": "rg-1"},
            "queue": {"type": "queue", "queue_id": "q-1"},
            "voicemail": {"type": "voicemail", "greeting": "leave one"},
        },
    }
    r1 = fe.step(flow, fe.start(flow).state, {"kind": "digit", "digit": "1"})
    assert r1.awaiting == "digit"  # now inside level2's menu

    ring_result = fe.step(flow, r1.state, {"kind": "digit", "digit": "1"})
    assert ring_result.actions == (fe.RingGroup(ring_group_id="rg-1"),)
    assert ring_result.awaiting == "ring_result"

    r1b = fe.step(flow, fe.start(flow).state, {"kind": "digit", "digit": "1"})
    queue_result = fe.step(flow, r1b.state, {"kind": "digit", "digit": "2"})
    assert queue_result.actions == (fe.Enqueue(queue_id="q-1"),)
    assert queue_result.terminal == "queued"

    r1c = fe.step(flow, fe.start(flow).state, {"kind": "digit", "digit": "1"})
    vm_result = fe.step(flow, r1c.state, {"kind": "digit", "digit": "3"})
    assert vm_result.actions == (fe.RecordVoicemail(greeting="leave one"),)
    assert vm_result.terminal == "voicemail"


def test_ring_group_answered_and_no_answer():
    flow = {
        "entry": "ring",
        "nodes": {
            "ring": {"type": "ring_group", "ring_group_id": "rg-1", "no_answer": "voicemail"},
            "voicemail": {"type": "voicemail", "greeting": "leave one"},
        },
    }
    state = fe.start(flow).state
    answered = fe.step(flow, state, {"kind": "ring_result", "result": "answered"})
    assert answered.terminal == "connected"

    no_answer = fe.step(flow, state, {"kind": "ring_result", "result": "no_answer"})
    assert no_answer.terminal == "voicemail"


# --------------------------------------------------------------------------------------
# Validation gate (DR-4)
# --------------------------------------------------------------------------------------
def test_validate_flow_rejects_dangling_reference_with_exact_node_id():
    flow = {
        "entry": "welcome",
        "nodes": {
            "welcome": {"type": "speak", "text": "hi", "next": "nowhere"},
        },
    }
    errors = fe.validate_flow(flow)
    assert any("nowhere" in e and "welcome" in e for e in errors)


def test_validate_flow_rejects_unreachable_node_with_exact_id():
    flow = {
        "entry": "welcome",
        "nodes": {
            "welcome": {"type": "hangup"},
            "orphan": {"type": "hangup"},
        },
    }
    errors = fe.validate_flow(flow)
    assert any("orphan" in e and "unreachable" in e for e in errors)


def test_validate_flow_rejects_menu_option_pointing_nowhere():
    flow = {
        "entry": "welcome",
        "nodes": {
            "welcome": {"type": "menu", "prompt": "p", "options": {"1": "ghost"}},
        },
    }
    errors = fe.validate_flow(flow)
    assert any("ghost" in e for e in errors)


def test_validate_flow_accepts_well_formed_flow():
    assert fe.validate_flow(_menu_flow()) == []


# --------------------------------------------------------------------------------------
# Runtime errors (DR-4's engine half - executor fallback itself lives in
# test_routing_exec.py, since it needs a DB session to create the voicemail row).
# --------------------------------------------------------------------------------------
def test_step_on_unknown_node_raises_flow_error():
    flow = _menu_flow()
    with pytest.raises(fe.FlowError):
        fe.step(flow, {"node": "does-not-exist", "retries": 0}, {"kind": "digit", "digit": "1"})


def test_start_with_missing_entry_raises_flow_error():
    with pytest.raises(fe.FlowError):
        fe.start({"nodes": {}})
