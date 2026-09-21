"""Unit tests for ``apply_evidence`` against a tiny ORM-like record."""

from __future__ import annotations

from app.compliance.telnyx_approval import (
    SOURCE_STATUS_DECISION,
    STATE_APPROVED,
    TELNYX_APPROVAL_KEY,
    apply_evidence,
)


class FakeRecord:
    """Minimal ORM-like stand-in exposing only ``carrier_refs``."""

    def __init__(self, carrier_refs: object = None) -> None:
        self.carrier_refs = carrier_refs


def _evidence(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "state": STATE_APPROVED,
        "carrier_id": "carrier-123",
        "checked_at": "2024-01-01T00:00:00+00:00",
        "source": SOURCE_STATUS_DECISION,
    }
    data.update(overrides)
    return data


def test_preserves_unrelated_refs_and_reassigns_fresh_dict() -> None:
    original = {"other": {"keep": True}}
    record = FakeRecord(original)

    result = apply_evidence(record, _evidence(carrier_id="  carrier-123  "))

    assert result is record
    assert record.carrier_refs is not original
    assert original == {"other": {"keep": True}}
    assert record.carrier_refs["other"], "unrelated refs must survive"
    assert record.carrier_refs["other"] == {"keep": True}


def test_stores_normalized_evidence_without_mutating_originals() -> None:
    original_refs = {"other": 1}
    evidence = _evidence(
        carrier_id="  carrier-123  ",
        checked_at="2024-01-01T00:00:00Z",
    )
    record = FakeRecord(original_refs)

    apply_evidence(record, evidence)

    assert evidence["carrier_id"] == "  carrier-123  "
    assert evidence["checked_at"] == "2024-01-01T00:00:00Z"
    assert original_refs == {"other": 1}

    stored = record.carrier_refs[TELNYX_APPROVAL_KEY]
    assert stored is not evidence
    assert stored == {
        "state": STATE_APPROVED,
        "carrier_id": "carrier-123",
        "checked_at": "2024-01-01T00:00:00+00:00",
        "source": SOURCE_STATUS_DECISION,
    }


def test_none_refs_starts_a_fresh_mapping() -> None:
    record = FakeRecord(None)
    apply_evidence(record, _evidence())
    assert set(record.carrier_refs) == {TELNYX_APPROVAL_KEY}


def test_none_record_returns_none() -> None:
    assert apply_evidence(None, _evidence()) is None


def test_rejects_malformed_evidence() -> None:
    malformed = [
        _evidence(state="bogus"),
        _evidence(source="bogus"),
        _evidence(carrier_id="   "),
        _evidence(checked_at="2024-01-01T00:00:00"),
        {},
    ]
    for bad in malformed:
        record = FakeRecord({"other": 1})
        try:
            apply_evidence(record, bad)
        except ValueError:
            pass
        else:  # pragma: no cover - guard
            raise AssertionError(f"expected ValueError for {bad!r}")
        assert record.carrier_refs == {"other": 1}


def test_rejects_non_mapping_inputs() -> None:
    record = FakeRecord({})
    for bad in (None, "state=approved", 3):
        try:
            apply_evidence(record, bad)  # type: ignore[arg-type]
        except TypeError:
            pass
        else:  # pragma: no cover - guard
            raise AssertionError(f"expected TypeError for {bad!r}")

    broken = FakeRecord([("a", 1)])
    try:
        apply_evidence(broken, _evidence())
    except TypeError:
        pass
    else:  # pragma: no cover - guard
        raise AssertionError("expected TypeError for non-mapping carrier_refs")
