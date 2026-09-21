"""Tests for the Telnyx approval evidence parser and builder."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.compliance.telnyx_approval import (
    SOURCE_REFRESH,
    SOURCE_STATUS_DECISION,
    STATE_APPROVED,
    STATE_REVOKED,
    TELNYX_APPROVAL_KEY,
    build_evidence,
    parse_evidence,
)

VALID = {
    "state": STATE_APPROVED,
    "carrier_id": "carrier-1",
    "checked_at": "2024-01-01T00:00:00+00:00",
    "source": SOURCE_STATUS_DECISION,
}


def refs(**overrides):
    return {TELNYX_APPROVAL_KEY: {**VALID, **overrides}}


@pytest.mark.parametrize(
    "carrier_refs",
    [None, [], {}, {TELNYX_APPROVAL_KEY: None}, {TELNYX_APPROVAL_KEY: "x"}],
    ids=["none", "list", "empty", "nonmapping", "string"],
)
def test_parse_missing_or_nonmapping(carrier_refs):
    assert parse_evidence(carrier_refs) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "pending"},
        {"state": None},
        {"source": "manual"},
        {"source": 3},
        {"carrier_id": "   "},
        {"carrier_id": 123},
        {"checked_at": "not-a-date"},
        {"checked_at": "2024-01-01T00:00:00"},
        {"checked_at": 1234},
        {"checked_at": ""},
    ],
)
def test_parse_invalid_fields(overrides):
    assert parse_evidence(refs(**overrides)) is None


@pytest.mark.parametrize(
    ("checked_at", "expected"),
    [
        (
            datetime(2024, 1, 1, 12, tzinfo=timezone(timedelta(hours=2))),
            "2024-01-01T10:00:00+00:00",
        ),
        ("2024-01-01T12:00:00Z", "2024-01-01T12:00:00+00:00"),
        ("2024-01-01T13:00:00+01:00", "2024-01-01T12:00:00+00:00"),
    ],
)
def test_build_normalizes_to_utc(checked_at, expected):
    built = build_evidence(
        state=STATE_APPROVED,
        carrier_id="carrier-1",
        checked_at=checked_at,
        source=SOURCE_REFRESH,
    )
    assert built["checked_at"] == expected
    assert built["state"] == STATE_APPROVED
    assert built["source"] == SOURCE_REFRESH


def test_build_then_parse_round_trip():
    built = build_evidence(
        state=STATE_REVOKED,
        carrier_id="  carrier-9  ",
        checked_at="2024-05-06T07:08:09+02:00",
        source=SOURCE_STATUS_DECISION,
    )
    parsed = parse_evidence({TELNYX_APPROVAL_KEY: built})
    assert parsed is not None
    assert parsed.as_dict() == {
        "state": STATE_REVOKED,
        "carrier_id": "carrier-9",
        "checked_at": "2024-05-06T05:08:09+00:00",
        "source": SOURCE_STATUS_DECISION,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("state", "pending"),
        ("state", None),
        ("source", "manual"),
        ("source", 5),
        ("carrier_id", "   "),
        ("carrier_id", 7),
        ("checked_at", "not-a-date"),
        ("checked_at", "2024-01-01T00:00:00"),
        ("checked_at", datetime(2024, 1, 1)),
        ("checked_at", 1),
    ],
)
def test_build_raises_on_invalid_field(field, value):
    with pytest.raises(ValueError):
        build_evidence(**{**VALID, field: value})
