"""Tests for :func:`evaluate_evidence` reason and ordering semantics.

Every datetime is a fixed UTC instant and the module under test is pure, so
these tests perform no I/O and never touch customer or phone data.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from app.compliance.telnyx_approval import (
    REASON_APPROVED,
    REASON_CARRIER_MISMATCH,
    REASON_FUTURE,
    REASON_INVALID_CARRIER_ID,
    REASON_INVALID_MAX_AGE,
    REASON_INVALID_NOW,
    REASON_MALFORMED,
    REASON_MISSING,
    REASON_REVOKED,
    REASON_STALE,
    SOURCE_REFRESH,
    STATE_APPROVED,
    STATE_REVOKED,
    TELNYX_APPROVAL_KEY,
    evaluate_evidence,
)

NOW = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
CARRIER_ID = "carrier-abc"
MAX_AGE = 3600


def make_refs(
    *,
    state: object = STATE_APPROVED,
    carrier_id: object = CARRIER_ID,
    checked_at: object = NOW,
    source: object = SOURCE_REFRESH,
) -> dict:
    """Build a ``carrier_refs`` mapping around a single approval payload."""
    if isinstance(checked_at, datetime):
        checked_at = checked_at.isoformat()
    return {
        TELNYX_APPROVAL_KEY: {
            "state": state,
            "carrier_id": carrier_id,
            "checked_at": checked_at,
            "source": source,
        }
    }


def evaluate(
    refs: object,
    *,
    now: object = NOW,
    max_age_seconds: object = MAX_AGE,
    carrier_id: object = CARRIER_ID,
) -> tuple[bool, str]:
    return evaluate_evidence(
        refs, carrier_id=carrier_id, now=now, max_age_seconds=max_age_seconds
    )


@pytest.mark.parametrize(
    "refs",
    [None, "not-a-mapping", [], {}, {"other": {"state": STATE_APPROVED}}],
)
def test_missing_evidence(refs: object) -> None:
    assert evaluate(refs) == (False, REASON_MISSING)


@pytest.mark.parametrize(
    "refs",
    [
        {TELNYX_APPROVAL_KEY: "not-a-mapping"},
        {TELNYX_APPROVAL_KEY: {}},
        make_refs(state="pending"),
        make_refs(state=STATE_REVOKED, source="manual"),
        make_refs(carrier_id=""),
        make_refs(carrier_id="   "),
        make_refs(carrier_id=123),
        make_refs(checked_at="not-a-timestamp"),
        make_refs(checked_at=""),
        make_refs(checked_at="2024-05-01T12:00:00"),
        make_refs(checked_at=123),
    ],
)
def test_malformed_evidence(refs: object) -> None:
    assert evaluate(refs) == (False, REASON_MALFORMED)


@pytest.mark.parametrize("carrier_id", ["", "   ", 123, None])
def test_invalid_or_blank_expected_carrier_id(carrier_id: object) -> None:
    assert evaluate(make_refs(), carrier_id=carrier_id) == (
        False,
        REASON_INVALID_CARRIER_ID,
    )


@pytest.mark.parametrize(
    "evidence_carrier_id", ["other", "Carrier-abc", CARRIER_ID + "-x"]
)
def test_evidence_carrier_id_mismatch(evidence_carrier_id: str) -> None:
    assert evaluate(make_refs(carrier_id=evidence_carrier_id)) == (
        False,
        REASON_CARRIER_MISMATCH,
    )


@pytest.mark.parametrize(
    "now",
    [
        datetime(2024, 5, 1, 12, 0, 0),  # naive datetime
        "2024-05-01T12:00:00+00:00",  # string, not a datetime
        None,
        12345,
    ],
)
def test_naive_or_invalid_now(now: object) -> None:
    assert evaluate(make_refs(), now=now) == (False, REASON_INVALID_NOW)


@pytest.mark.parametrize(
    "max_age_seconds",
    [True, False, 3600.0, 0.0, 0, -1, "3600", None],
)
def test_invalid_max_age_seconds(max_age_seconds: object) -> None:
    assert evaluate(make_refs(), max_age_seconds=max_age_seconds) == (
        False,
        REASON_INVALID_MAX_AGE,
    )


def test_future_timestamp_is_rejected() -> None:
    refs = make_refs(checked_at=NOW + timedelta(seconds=1))
    assert evaluate(refs) == (False, REASON_FUTURE)


def test_stale_timestamp_is_rejected() -> None:
    refs = make_refs(checked_at=NOW - timedelta(seconds=MAX_AGE + 1))
    assert evaluate(refs) == (False, REASON_STALE)


def test_revoked_evidence_is_rejected() -> None:
    refs = make_refs(state=STATE_REVOKED, checked_at=NOW - timedelta(seconds=10))
    assert evaluate(refs) == (False, REASON_REVOKED)


def test_fresh_approved_evidence_is_accepted() -> None:
    refs = make_refs(checked_at=NOW - timedelta(seconds=10))
    assert evaluate(refs) == (True, REASON_APPROVED)


def test_exact_max_age_boundary_is_accepted() -> None:
    refs = make_refs(checked_at=NOW - timedelta(seconds=MAX_AGE))
    assert evaluate(refs) == (True, REASON_APPROVED)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # Stale is checked before revoked: a stale revoked record reports stale.
        (
            {"state": STATE_REVOKED, "checked_at": NOW - timedelta(seconds=MAX_AGE + 1)},
            REASON_STALE,
        ),
        # Future is checked before revoked as well.
        (
            {"state": STATE_REVOKED, "checked_at": NOW + timedelta(seconds=1)},
            REASON_FUTURE,
        ),
        # A mismatched evidence id wins over staleness.
        ({"checked_at": NOW - timedelta(days=30)}, REASON_CARRIER_MISMATCH),
        # An invalid max_age beats stale/future timestamp checks.
        (
            {"max_age_seconds": 0, "checked_at": NOW - timedelta(days=30)},
            REASON_INVALID_MAX_AGE,
        ),
        # An invalid now beats an invalid max_age.
        ({"now": "bad", "max_age_seconds": 0}, REASON_INVALID_NOW),
    ],
)
def test_reason_ordering(overrides: dict, expected: str) -> None:
    options = dict(overrides)
    if expected == REASON_CARRIER_MISMATCH:
        options["refs_carrier_id"] = "other-carrier"
    refs_carrier_id = options.pop("refs_carrier_id", None)
    refs_kwargs = {
        "state": options.pop("state", STATE_APPROVED),
        "carrier_id": refs_carrier_id if refs_carrier_id is not None else CARRIER_ID,
        "checked_at": options.pop("checked_at", NOW),
    }
    assert evaluate(make_refs(**refs_kwargs), **options) == (False, expected)


@pytest.mark.parametrize(
    "refs",
    [
        {},
        {TELNYX_APPROVAL_KEY: {}},
        make_refs(),
        make_refs(state=STATE_REVOKED, checked_at=NOW - timedelta(seconds=MAX_AGE + 1)),
    ],
)
def test_inputs_are_not_mutated(refs: object) -> None:
    snapshot = copy.deepcopy(refs)
    evaluate(refs)
    assert refs == snapshot
