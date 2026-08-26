from __future__ import annotations

import pytest

from app.services.messaging import apply_event, is_conflicting_terminal


@pytest.mark.parametrize(
    "current,event,expected",
    [
        ("queued", "message-sending", "sending"),
        ("queued", "message-delivered", "delivered"),
        ("queued", "message-failed", "failed"),
        ("accepted", "message-sending", "sending"),
        ("accepted", "message-delivered", "delivered"),
        ("sending", "message-delivered", "delivered"),
        ("sending", "message-failed", "failed"),
        # rank must strictly increase — no going backwards
        ("sending", "message-sending", None),
        ("delivered", "message-sending", None),
        ("delivered", "message-delivered", None),
        # terminals are immutable
        ("delivered", "message-failed", None),
        ("failed", "message-delivered", None),
        ("rejected", "message-delivered", None),
        # unknown event types never transition
        ("queued", "message-unknown", None),
    ],
)
def test_transition_table(current, event, expected):
    assert apply_event(current, event) is expected or apply_event(current, event) == expected


def test_delivered_before_sending():
    """This WILL happen in production — Bandwidth retries are unordered and parallel."""
    status = "accepted"
    status = apply_event(status, "message-delivered") or status
    assert status == "delivered"
    # The late `sending` must NOT regress the status...
    assert apply_event(status, "message-sending") is None
    # ...but the caller still ledgers the event, so the audit trail stays complete.


def test_terminal_immutable_first_wins():
    assert apply_event("delivered", "message-failed") is None
    assert apply_event("failed", "message-delivered") is None


def test_conflicting_terminal_is_signalled():
    assert is_conflicting_terminal("delivered", "message-failed") is True
    assert is_conflicting_terminal("failed", "message-delivered") is True
    assert is_conflicting_terminal("delivered", "message-delivered") is False
    assert is_conflicting_terminal("sending", "message-delivered") is False
