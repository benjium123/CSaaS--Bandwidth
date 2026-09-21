"""Bounded Telnyx approval evidence, as judged by the pre-send gate.

Everything here drives ``app.compliance.registration`` directly against lightweight model
instances and a fake async session: no database, carrier or network is touched, and the
clock and freshness window are supplied explicitly. The load-bearing property is that
every bad Telnyx approval-evidence shape - missing, malformed, stale, future-dated,
revoked or a mismatched carrier id - collapses to a single operator-facing refusal, while
a locally approved non-Telnyx number never pays for reading the Telnyx settings or clock.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.compliance import registration as reg
from app.compliance.telnyx_approval import build_evidence
from app.models import OrgNumber
from app.models.numbers import Campaign, TollFreeVerification

NOW = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
MAX_AGE = 3600
FRESH = NOW - timedelta(seconds=30)
CARRIER_ID = "telnyx-campaign-1"
EVIDENCE_CASES = ("missing", "malformed", "stale", "future", "revoked", "mismatch")


class _Settings:
    telnyx_approval_max_age_seconds = MAX_AGE


class _Result:
    """The slice of a Result the gate actually consumes."""

    def __init__(self, rows):
        self._rows = list(rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    """Enough of an AsyncSession for the rows these code paths load.

    Counts accesses so tests can prove the Telnyx checks read only already-loaded rows
    and add no per-number round trip.
    """

    def __init__(self, *, campaigns=(), tfvs=()):
        self.campaigns = {c.id: c for c in campaigns}
        self.tfvs = {t.number_id: t for t in tfvs}
        self.execute_calls = 0
        self.get_calls = 0

    async def get(self, model, pk):
        self.get_calls += 1
        return self.campaigns.get(pk) if model is Campaign else None

    async def execute(self, stmt):
        self.execute_calls += 1
        entity = stmt.column_descriptions[0].get("entity")
        if entity is Campaign:
            return _Result(self.campaigns.values())
        if entity is TollFreeVerification:
            return _Result(self.tfvs.values())
        raise AssertionError(f"unexpected query: {stmt}")


def _evidence(*, state="approved", carrier_id=CARRIER_ID, checked_at=FRESH):
    return build_evidence(
        state=state,
        carrier_id=carrier_id,
        checked_at=checked_at,
        source="status_decision",
    )


def _approved_refs(*, state="approved", carrier_id=CARRIER_ID, checked_at=FRESH):
    return {
        "telnyx": CARRIER_ID,
        "telnyx_approval": _evidence(
            state=state, carrier_id=carrier_id, checked_at=checked_at
        ),
    }


def _failure_refs(case):
    if case == "missing":
        return {"telnyx": CARRIER_ID}
    if case == "malformed":
        return {"telnyx": CARRIER_ID, "telnyx_approval": {"state": "approved"}}
    if case == "stale":
        return _approved_refs(checked_at=NOW - timedelta(seconds=MAX_AGE + 600))
    if case == "revoked":
        return _approved_refs(state="revoked")
    if case == "future":
        return _approved_refs(checked_at=NOW + timedelta(seconds=600))
    if case == "mismatch":
        return _approved_refs(carrier_id="other-carrier-id")
    raise AssertionError(case)


def _campaign(*, refs, status="approved"):
    return Campaign(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        brand_id=uuid.uuid4(),
        name="Camp",
        status=status,
        carrier_refs=refs,
    )


def _number(
    *,
    carrier="telnyx",
    campaign_id=None,
    e164="+12145550000",
    number_type="local",
    is_active=True,
    status="active",
):
    return OrgNumber(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        e164=e164,
        number_type=number_type,
        carrier=carrier,
        status=status,
        is_active=is_active,
        campaign_id=campaign_id,
        provisioning={},
    )


def _assigned_marker(campaign):
    """A full ``OrgNumber.provisioning`` mapping carrying a completed assignment."""
    return {
        "telnyx_campaign_assignment": {
            "state": "assigned",
            "campaign_id": str(campaign.id),
            "carrier_id": CARRIER_ID,
        }
    }


def _marker_variant(name, campaign):
    """A full ``OrgNumber.provisioning`` mapping for each non-happy assignment shape."""
    if name == "absent":
        return {}
    if name == "incomplete":
        marker = {
            "state": "pending",
            "campaign_id": str(campaign.id),
            "carrier_id": CARRIER_ID,
        }
    elif name == "wrong_campaign":
        marker = {
            "state": "assigned",
            "campaign_id": str(uuid.uuid4()),
            "carrier_id": CARRIER_ID,
        }
    elif name == "wrong_carrier":
        marker = {
            "state": "assigned",
            "campaign_id": str(campaign.id),
            "carrier_id": "some-other-carrier",
        }
    else:
        raise AssertionError(name)
    return {"telnyx_campaign_assignment": marker}


def _telnyx_registration(kind, refs):
    """Build (session, number, registration) for a locally approved Telnyx number."""
    if kind == "local":
        campaign = _campaign(refs=refs)
        number = _number(campaign_id=campaign.id)
        number.provisioning = _assigned_marker(campaign)
        return _FakeSession(campaigns=[campaign]), number, campaign
    number = _number(number_type="tollfree", e164="+18885550000")
    tfv = TollFreeVerification(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        number_id=number.id,
        business_name="B",
        status="approved",
        carrier_refs=refs,
    )
    return _FakeSession(tfvs=[tfv]), number, tfv


@pytest.mark.parametrize("kind", ["local", "tollfree"])
async def test_fresh_matching_evidence_permits(kind):
    session, number, registration = _telnyx_registration(kind, _approved_refs())
    state = await reg.registration_state(session, number, max_age_seconds=MAX_AGE, now=NOW)
    allowed, reason = await reg.check_number_may_send(
        session, number.org_id, number, max_age_seconds=MAX_AGE, now=NOW
    )
    assert state.verdict == "approved"
    assert allowed is True
    assert reason == ""
    assert registration.status == "approved"


@pytest.mark.parametrize("kind", ["local", "tollfree"])
async def test_every_bad_evidence_shape_is_one_safe_refusal(kind):
    details = set()
    for case in EVIDENCE_CASES:
        session, number, registration = _telnyx_registration(kind, _failure_refs(case))
        state = await reg.registration_state(
            session, number, max_age_seconds=MAX_AGE, now=NOW
        )
        allowed, reason = await reg.check_number_may_send(
            session, number.org_id, number, max_age_seconds=MAX_AGE, now=NOW
        )
        assert state.verdict == "pending", case
        assert allowed is False, case
        assert "cannot send" in reason, case
        assert CARRIER_ID not in reason, f"{case}: internal carrier id must not leak"
        assert registration.status == "approved", f"{case}: approval must not downgrade"
        details.add(state.detail)
    assert details and "" not in details
    assert len(details) == 1, "carrier-internal detail must not reach the operator"


async def test_assignment_marker_is_judged_before_evidence():
    # No evidence at all is on file: were evidence judged first the refusal would be the
    # generic one. The marker runs first, so the operator is told the assignment is absent.
    campaign = _campaign(refs={"telnyx": CARRIER_ID})
    number = _number(campaign_id=campaign.id)  # provisioning == {} -> no marker
    session = _FakeSession(campaigns=[campaign])
    state = await reg.registration_state(session, number, max_age_seconds=MAX_AGE, now=NOW)
    assert state.verdict == "pending"
    assert state.detail != reg._TELNYX_EVIDENCE_REFUSAL
    assert "assignment" in state.detail


@pytest.mark.parametrize(
    ("variant", "expected"),
    [
        ("absent", "no Telnyx campaign assignment"),
        ("incomplete", "not complete"),
        ("wrong_campaign", "does not match this campaign"),
        ("wrong_carrier", "does not match the carrier campaign"),
    ],
)
async def test_incomplete_assignment_markers_refuse(variant, expected):
    campaign = _campaign(refs={"telnyx": CARRIER_ID})  # evidence deliberately absent
    number = _number(campaign_id=campaign.id)
    number.provisioning = _marker_variant(variant, campaign)
    session = _FakeSession(campaigns=[campaign])
    state = await reg.registration_state(session, number, max_age_seconds=MAX_AGE, now=NOW)
    assert state.verdict == "pending"
    assert expected in state.detail
    assert state.detail != reg._TELNYX_EVIDENCE_REFUSAL


async def test_local_campaign_without_a_carrier_ref_refuses():
    campaign = _campaign(refs={})  # approved locally, nothing registered at Telnyx
    number = _number(campaign_id=campaign.id)
    number.provisioning = _assigned_marker(campaign)
    session = _FakeSession(campaigns=[campaign])
    state = await reg.registration_state(session, number, max_age_seconds=MAX_AGE, now=NOW)
    assert state.verdict == "pending"
    assert "not registered with Telnyx" in state.detail
    assert state.detail != reg._TELNYX_EVIDENCE_REFUSAL


async def test_non_telnyx_numbers_never_resolve_telnyx_settings(monkeypatch):
    def _boom():
        raise AssertionError("Telnyx settings must not be read for non-Telnyx numbers")

    monkeypatch.setattr(reg, "get_active_settings", _boom)

    campaign = _campaign(refs={})
    number = _number(carrier="bandwidth", campaign_id=campaign.id)
    session = _FakeSession(campaigns=[campaign])
    state = await reg.registration_state(session, number)
    assert state.verdict == "approved"
    assert await reg.check_number_may_send(session, number.org_id, number) == (True, "")

    pending = _campaign(refs={}, status="pending")
    pending_number = _number(carrier="bandwidth", campaign_id=pending.id)
    pending_session = _FakeSession(campaigns=[pending])
    allowed, reason = await reg.check_number_may_send(
        pending_session, pending_number.org_id, pending_number
    )
    assert allowed is False
    assert "pending" in reason

    tfv_number = _number(carrier="bandwidth", number_type="tollfree", e164="+18885550001")
    tfv = TollFreeVerification(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        number_id=tfv_number.id,
        business_name="B",
        status="approved",
        carrier_refs={},
    )
    tf_session = _FakeSession(tfvs=[tfv])
    assert await reg.check_number_may_send(
        tf_session, tfv_number.org_id, tfv_number
    ) == (True, "")


async def test_inactive_number_short_circuits_before_settings(monkeypatch):
    def _boom():
        raise AssertionError("an inactive number must not resolve Telnyx settings")

    monkeypatch.setattr(reg, "get_active_settings", _boom)

    for number in (_number(is_active=False), _number(status="released")):
        campaign = _campaign(refs=_approved_refs())
        number.campaign_id = campaign.id
        number.provisioning = _assigned_marker(campaign)
        session = _FakeSession(campaigns=[campaign])
        allowed, reason = await reg.check_number_may_send(
            session, number.org_id, number
        )
        assert allowed is False
        assert "cannot send" in reason


async def test_batch_without_telnyx_never_resolves_telnyx_settings(monkeypatch):
    def _boom():
        raise AssertionError("a pool with no Telnyx number must not read Telnyx settings")

    monkeypatch.setattr(reg, "get_active_settings", _boom)

    campaign = _campaign(refs={})
    registered = _number(carrier="bandwidth", campaign_id=campaign.id)
    unregistered = _number(carrier="bandwidth", e164="+12145550001")
    session = _FakeSession(campaigns=[campaign])
    allowed, refused = await reg.partition_by_eligibility(session, [registered, unregistered])
    assert allowed == [registered, unregistered]
    assert refused == {}


async def test_telnyx_batch_resolves_settings_and_clock_once(monkeypatch):
    calls = {"settings": 0, "clock": 0}

    def _settings():
        calls["settings"] += 1
        return _Settings()

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            calls["clock"] += 1
            return NOW

    monkeypatch.setattr(reg, "get_active_settings", _settings)
    monkeypatch.setattr(reg, "datetime", _Clock)

    campaign = _campaign(refs=_approved_refs())
    first = _number(campaign_id=campaign.id)
    first.provisioning = _assigned_marker(campaign)
    second = _number(campaign_id=campaign.id, e164="+12145550001")
    second.provisioning = _assigned_marker(campaign)
    session = _FakeSession(campaigns=[campaign])

    allowed, refused = await reg.partition_by_eligibility(session, [first, second])

    assert allowed == [first, second]
    assert refused == {}
    assert calls == {"settings": 1, "clock": 1}, "resolved once, not per number"
    assert session.execute_calls == 1, "evidence must read already-loaded rows only"
    assert session.get_calls == 0
