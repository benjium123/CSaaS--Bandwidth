"""P44 review fixes: flow transfers pass the destination firewall; dialer refusals are row
outcomes, never a wave-aborting exception."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.db.base import set_org_context
from app.errors import ValidationFailedError
from app.providers import voice
from app.providers.voice import VoiceEvent
from app.services import dialer, flows
from app.services import routing_exec as routing_exec_svc
from tests.test_routing_exec import OUR, FakeBus, _flow_row, _make_call, _make_org

JAMAICA = "+18765551234"


def _transfer_flow(to: str) -> dict:
    return {
        "entry": "root",
        "nodes": {
            "root": {
                "type": "menu",
                "prompt": "press 1",
                "options": {"1": "xfer"},
                "invalid_node": "xfer",
            },
            "xfer": {"type": "transfer", "to": to},
        },
    }


async def test_a_flow_cannot_be_saved_with_an_irsf_transfer(session):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    with pytest.raises(ValidationFailedError, match="cannot call"):
        await flows.validate_and_raise(session, org_id, _transfer_flow(JAMAICA))
    await flows.validate_and_raise(session, org_id, _transfer_flow("+15125551234"))


async def test_a_saved_irsf_transfer_hangs_up_instead(session, engine):
    org_id = await _make_org(session)
    set_org_context(session, org_id)
    flow = await _flow_row(session, org_id, _transfer_flow("+15125551234"))
    flow.definition = _transfer_flow(JAMAICA)  # as if saved before P44
    await session.flush()
    call = _make_call(org_id)
    session.add(call)
    await session.flush()
    bus = FakeBus()
    await routing_exec_svc.start_carrier_flow(session, bus, call, flow)
    commands = await routing_exec_svc.continue_carrier_flow(
        session,
        bus,
        call,
        VoiceEvent(
            event_type="dtmf_received", provider_call_id="x", provider_event_id="a", digits="1"
        ),
    )
    assert not any(isinstance(c, voice.Transfer) for c in commands)
    assert isinstance(commands[-1], voice.Hangup)
    assert OUR  # the helper module's number is untouched


def _row():
    return SimpleNamespace(attempts=0, status="dialing", disposition=None,
                           call_id=None, amd_verdict=None, next_attempt_at=None)


def test_a_refused_destination_is_a_terminal_row_outcome():
    campaign = SimpleNamespace(max_attempts=3, retry_backoff_minutes=60)
    row = _row()
    now = datetime.now(timezone.utc)
    key = dialer._apply_outcome(
        row, dialer.DialOutcome(status="failed", refused="destination_not_allowed"), campaign, now
    )
    assert key == "failed" and row.status == "failed" and row.disposition == "blocked"
    assert row.attempts == 0


def test_an_account_level_refusal_waits_without_using_an_attempt():
    campaign = SimpleNamespace(max_attempts=3, retry_backoff_minutes=60)
    row = _row()
    now = datetime.now(timezone.utc)
    key = dialer._apply_outcome(
        row, dialer.DialOutcome(status="failed", refused="daily_spend_reached"), campaign, now
    )
    assert key == "retry_scheduled" and row.status == "queued" and row.attempts == 0
    assert row.next_attempt_at > now


async def test_ops_limits_form_edits_keep_the_p44_limits(session):
    import uuid as _uuid

    from app.models import KycProfile
    from app.services import kyc

    org_id = await _make_org(session)
    set_org_context(session, org_id)
    profile = KycProfile(id=_uuid.uuid4(), org_id=org_id, status="approved", country="US")
    session.add(profile)
    await session.flush()
    await kyc.set_limits(session, profile, None, deposit_required_cents=None,
                         limits={"established": True, "max_concurrent_calls": 12})
    # The legacy form sends only its own three keys.
    await kyc.set_limits(session, profile, None, deposit_required_cents=None,
                         limits={"daily_calls": 500})
    assert profile.limits == {"daily_calls": 500, "established": True, "max_concurrent_calls": 12}
    with pytest.raises(ValidationFailedError):
        await kyc.set_limits(session, profile, None, deposit_required_cents=None,
                             limits={"established": "yes"})
