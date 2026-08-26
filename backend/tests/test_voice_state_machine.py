"""Phase 5: the voice state machine - monotonic-by-rank, same shape as messages, media and
registration (D6: carriers retry unordered, and a late webhook must never walk a fact
backwards).

Pure unit tests: no DB, no session, model objects constructed and pushed straight through
calls_svc's pure functions - the property under test is arithmetic on ranks, mirroring
test_numbers_registration.py's style for exactly the same reason.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.models.voice import Call, CallLeg
from app.services import calls as calls_svc


def _leg(**overrides) -> CallLeg:
    defaults = {
        "id": uuid.uuid4(),
        "org_id": uuid.uuid4(),
        "call_id": uuid.uuid4(),
        "to_e164": "+19725550199",
        "from_e164": "+12145550100",
        "reason": "original",
    }
    defaults.update(overrides)
    return CallLeg(**defaults)


def _call(**overrides) -> Call:
    defaults = {
        "id": uuid.uuid4(),
        "org_id": uuid.uuid4(),
        "direction": "outbound",
        "contact_e164": "+19725550199",
        "our_e164": "+12145550100",
        "carrier": "bandwidth",
    }
    defaults.update(overrides)
    return Call(**defaults)


# ---------------------------------------------------------------------------------
# Leg rank
# ---------------------------------------------------------------------------------
def test_leg_rank_never_decreases():
    leg = _leg()
    leg.status = "ringing"
    assert calls_svc.advance_leg(leg, "answered") is True
    assert calls_svc.advance_leg(leg, "dialing") is False, "rank must never decrease"
    assert leg.status == "answered"


def test_leg_terminal_is_never_replaced_by_a_different_terminal():
    leg = _leg()
    leg.status = "hungup"
    assert calls_svc.advance_leg(leg, "failed") is False
    assert leg.status == "hungup"


def test_leg_answered_at_is_set_once_not_moved_by_a_duplicate():
    leg = _leg()
    leg.status = "ringing"
    calls_svc.advance_leg(leg, "answered")
    first = leg.answered_at
    assert first is not None
    assert calls_svc.advance_leg(leg, "answered") is False
    assert leg.answered_at == first


def test_out_of_order_hungup_before_answered_ignores_the_late_answer():
    leg = _leg()
    assert calls_svc.advance_leg(leg, "hungup") is True
    assert calls_svc.advance_leg(leg, "answered") is False
    assert leg.status == "hungup"
    assert leg.answered_at is None, "a leg that never truly answered must not look answered"


def test_unflushed_leg_status_none_is_treated_as_created():
    leg = _leg()
    assert leg.status is None  # a column default only applies at INSERT
    assert calls_svc.advance_leg(leg, "dialing") is True


def test_same_status_is_a_no_op_not_a_change():
    leg = _leg()
    leg.status = "ringing"
    assert calls_svc.advance_leg(leg, "ringing") is False


# ---------------------------------------------------------------------------------
# Call rank (via derive_call_status - a pure function of the legs)
# ---------------------------------------------------------------------------------
def test_call_status_tracks_the_most_advanced_leg_before_answer():
    call = _call()
    leg = _leg()
    leg.status = "ringing"
    assert calls_svc.derive_call_status(call, [leg]) is True
    assert call.status == "ringing"


def test_call_answered_when_any_leg_answers():
    call = _call()
    leg = _leg()
    leg.status = "answered"
    leg.answered_at = datetime.now(timezone.utc)
    assert calls_svc.derive_call_status(call, [leg]) is True
    assert call.status == "answered"
    assert call.answered_at == leg.answered_at


def test_transfer_second_leg_keeps_the_call_answered_while_the_old_leg_ends():
    """A single-row call model corrupts on exactly this case (phase-5-plan): the first
    leg's hangup must NOT complete the call while the transferred party is still on."""
    call = _call()
    leg1 = _leg(reason="original")
    leg1.status = "answered"
    leg1.answered_at = datetime.now(timezone.utc)
    calls_svc.derive_call_status(call, [leg1])
    assert call.status == "answered"

    leg2 = _leg(call_id=leg1.call_id, reason="transfer")
    leg1.status = "hungup"  # the old leg dies; a transfer never mutates it further
    calls_svc.derive_call_status(call, [leg1, leg2])
    assert call.status == "answered", "leg2 is still live - the call must stay up"
    assert call.ended_at is None

    leg2.status = "answered"
    leg2.answered_at = datetime.now(timezone.utc)
    calls_svc.derive_call_status(call, [leg1, leg2])
    assert call.status == "answered"

    leg2.status = "hungup"
    changed = calls_svc.derive_call_status(call, [leg1, leg2])
    assert changed is True
    assert call.status == "completed", "the call completes only when the LAST leg ends"
    assert call.ended_at is not None
    assert call.duration_seconds is not None


def test_call_terminal_is_never_replaced_by_a_different_terminal():
    call = _call()
    call.status = "completed"  # already terminal, and never actually answered
    leg = _leg()
    leg.status = "failed"  # would derive to "failed" - a DIFFERENT terminal
    assert calls_svc.derive_call_status(call, [leg]) is False
    assert call.status == "completed"


def test_call_all_legs_failed_without_ever_answering_is_failed():
    call = _call()
    leg = _leg()
    leg.status = "failed"
    calls_svc.derive_call_status(call, [leg])
    assert call.status == "failed"
    assert call.duration_seconds is None


def test_call_terminal_without_answer_or_all_failed_is_no_answer():
    call = _call()
    leg1 = _leg()
    leg1.status = "hungup"  # e.g. rang out, nobody picked up
    leg2 = _leg(call_id=leg1.call_id)
    leg2.status = "failed"
    calls_svc.derive_call_status(call, [leg1, leg2])
    assert call.status == "no_answer"


# ---------------------------------------------------------------------------------
# F8: hangup-cause -> terminal flavor (busy / canceled / plain failed)
# ---------------------------------------------------------------------------------
def test_leg_terminal_status_for_busy_and_cancel_causes_is_failed_not_hungup():
    """advance_leg itself only knows hungup/failed - the busy/canceled FLAVOR is a
    call-level derivation read back off hangup_cause, never a leg status of its own."""
    assert calls_svc._leg_terminal_status_for_cause("busy") == "failed"  # noqa: SLF001
    assert calls_svc._leg_terminal_status_for_cause("user_busy") == "failed"  # noqa: SLF001
    assert calls_svc._leg_terminal_status_for_cause("cancel") == "failed"  # noqa: SLF001
    assert calls_svc._leg_terminal_status_for_cause("originator_cancel") == "failed"  # noqa: SLF001
    assert calls_svc._leg_terminal_status_for_cause("rejected") == "failed"  # noqa: SLF001
    assert calls_svc._leg_terminal_status_for_cause("call_rejected") == "failed"  # noqa: SLF001
    assert calls_svc._leg_terminal_status_for_cause("normal-clearing") == "hungup"  # noqa: SLF001
    assert calls_svc._leg_terminal_status_for_cause("") == "hungup"  # noqa: SLF001


def test_call_all_legs_busy_cause_is_busy():
    call = _call()
    leg = _leg()
    leg.status = "failed"
    leg.hangup_cause = "busy"
    calls_svc.derive_call_status(call, [leg])
    assert call.status == "busy"


def test_call_all_legs_cancel_cause_is_canceled():
    call = _call()
    leg = _leg()
    leg.status = "failed"
    leg.hangup_cause = "originator_cancel"
    calls_svc.derive_call_status(call, [leg])
    assert call.status == "canceled"


def test_call_rejected_cause_is_plain_failed_not_canceled():
    """"rejected"/"call_rejected" are in the failed set but are NOT "cancel-ish" - only
    cancel/originator_cancel earn the "canceled" flavor."""
    call = _call()
    leg = _leg()
    leg.status = "failed"
    leg.hangup_cause = "rejected"
    calls_svc.derive_call_status(call, [leg])
    assert call.status == "failed"


def test_call_mixed_busy_and_never_answered_cause_is_no_answer_not_busy():
    """"every terminal leg's cause is busy-ish" - a mixed transfer where one leg was
    merely never answered must not masquerade as a clean busy."""
    call = _call()
    leg1 = _leg()
    leg1.status = "failed"
    leg1.hangup_cause = "busy"
    leg2 = _leg(call_id=leg1.call_id, reason="transfer")
    leg2.status = "hungup"
    leg2.hangup_cause = "normal-clearing"
    calls_svc.derive_call_status(call, [leg1, leg2])
    assert call.status == "no_answer"


def test_bridged_is_monotonic_and_a_stale_rederive_does_not_regress_it():
    call = _call()
    call.status = "answered"
    call.answered_at = datetime.now(timezone.utc)
    assert calls_svc._advance_call(call, "bridged") is True  # noqa: SLF001

    leg = _leg()
    leg.status = "answered"
    leg.answered_at = call.answered_at
    # Re-deriving from a merely-answered leg must not walk "bridged" back to "answered".
    assert calls_svc.derive_call_status(call, [leg]) is False
    assert call.status == "bridged"


# ---------------------------------------------------------------------------------
# active_leg
# ---------------------------------------------------------------------------------
def test_active_leg_is_the_most_recent_non_terminal_leg():
    call_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    leg1 = _leg(call_id=call_id)
    leg1.status = "hungup"
    leg1.created_at = now
    leg2 = _leg(call_id=call_id, reason="transfer")
    leg2.status = "created"
    leg2.created_at = now + timedelta(seconds=1)
    assert calls_svc.active_leg([leg1, leg2]) is leg2


def test_active_leg_is_none_when_every_leg_is_terminal():
    leg = _leg()
    leg.status = "hungup"
    assert calls_svc.active_leg([leg]) is None


def test_active_leg_is_none_with_no_legs():
    assert calls_svc.active_leg([]) is None
