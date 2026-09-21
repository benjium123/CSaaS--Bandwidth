"""Pure helpers for Telnyx approval evidence stored in ``carrier_refs``.

This module is intentionally dependency-free: it performs no I/O, no
networking, no logging, and never touches customer or phone data. The
approval evidence lives under the ``telnyx_approval`` key of a record's
``carrier_refs`` mapping.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

TELNYX_APPROVAL_KEY = "telnyx_approval"

STATE_APPROVED = "approved"
STATE_REVOKED = "revoked"
_STATES: frozenset[str] = frozenset({STATE_APPROVED, STATE_REVOKED})

SOURCE_STATUS_DECISION = "status_decision"
SOURCE_REFRESH = "refresh"
_SOURCES: frozenset[str] = frozenset({SOURCE_STATUS_DECISION, SOURCE_REFRESH})

REASON_APPROVED = "approved"
REASON_MISSING = "missing"
REASON_MALFORMED = "malformed"
REASON_INVALID_CARRIER_ID = "invalid_carrier_id"
REASON_CARRIER_MISMATCH = "carrier_id_mismatch"
REASON_INVALID_NOW = "invalid_now"
REASON_INVALID_MAX_AGE = "invalid_max_age"
REASON_FUTURE = "future_timestamp"
REASON_STALE = "stale"
REASON_REVOKED = "revoked"

__all__ = [
    "TELNYX_APPROVAL_KEY",
    "STATE_APPROVED",
    "STATE_REVOKED",
    "SOURCE_STATUS_DECISION",
    "SOURCE_REFRESH",
    "TelnyxApprovalEvidence",
    "parse_evidence",
    "build_evidence",
    "apply_evidence",
    "evaluate_evidence",
]


@dataclass(frozen=True)
class TelnyxApprovalEvidence:
    """Validated, normalized evidence parsed from ``carrier_refs``."""

    state: str
    carrier_id: str
    checked_at: str
    source: str

    def as_dict(self) -> dict[str, str]:
        """Return a fresh, JSON-safe copy of the normalized evidence."""
        return {
            "state": self.state,
            "carrier_id": self.carrier_id,
            "checked_at": self.checked_at,
            "source": self.source,
        }


def _parse_utc(value: object) -> datetime | None:
    """Return ``value`` as timezone-aware UTC datetime, or ``None``."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed.astimezone(timezone.utc)


def _coerce_utc(value: object) -> datetime | None:
    """Return an aware datetime or an ISO string as UTC, else ``None``."""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            return None
        return value.astimezone(timezone.utc)
    return _parse_utc(value)


def _is_aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.tzinfo.utcoffset(value) is not None
    )


def parse_evidence(carrier_refs: object) -> TelnyxApprovalEvidence | None:
    """Parse approval evidence from ``carrier_refs``; total, never raises.

    Returns ``None`` for any malformed shape, state, source, empty/missing
    ``carrier_id`` or invalid ``checked_at``. ``checked_at`` must be a string
    parseable as timezone-aware ISO and is normalized to UTC.
    """
    if not isinstance(carrier_refs, Mapping):
        return None
    raw = carrier_refs.get(TELNYX_APPROVAL_KEY)
    if not isinstance(raw, Mapping):
        return None

    state = raw.get("state")
    if not isinstance(state, str) or state not in _STATES:
        return None

    source = raw.get("source")
    if not isinstance(source, str) or source not in _SOURCES:
        return None

    carrier_id = raw.get("carrier_id")
    if not isinstance(carrier_id, str) or not carrier_id.strip():
        return None

    checked = _parse_utc(raw.get("checked_at"))
    if checked is None:
        return None

    return TelnyxApprovalEvidence(
        state=state,
        carrier_id=carrier_id.strip(),
        checked_at=checked.isoformat(),
        source=source,
    )


def build_evidence(
    *,
    state: str,
    carrier_id: str,
    checked_at: datetime | str,
    source: str,
) -> dict[str, str]:
    """Validate arguments and build a JSON-safe evidence dict.

    ``checked_at`` is accepted as an aware datetime or a timezone-aware ISO
    string and normalized to a UTC ISO timestamp. Raises ``ValueError`` on any
    invalid argument.
    """
    if not isinstance(state, str) or state not in _STATES:
        raise ValueError(f"invalid state: {state!r}")
    if not isinstance(source, str) or source not in _SOURCES:
        raise ValueError(f"invalid source: {source!r}")
    if not isinstance(carrier_id, str) or not carrier_id.strip():
        raise ValueError("carrier_id must be a non-empty string")

    checked = _coerce_utc(checked_at)
    if checked is None:
        raise ValueError("checked_at must be a timezone-aware ISO timestamp")

    return {
        "state": state,
        "carrier_id": carrier_id.strip(),
        "checked_at": checked.isoformat(),
        "source": source,
    }


def apply_evidence(record: object, evidence: Mapping[str, Any]) -> object | None:
    """Validate ``evidence`` and reassign ``record.carrier_refs``.

    Reads ``record.carrier_refs``, copies the mapping, sets a copied, validated
    evidence payload, and assigns the new dict back to ``record.carrier_refs``
    so that a SQLAlchemy ``PortableJSON`` column detects the reassignment. The
    existing mapping is never mutated in place. Returns the record (or ``None``
    when ``record`` is ``None``). Raises ``ValueError`` for invalid evidence and
    ``TypeError`` when ``record.carrier_refs`` is not a mapping.
    """
    if record is None:
        return None
    if not isinstance(evidence, Mapping):
        raise TypeError("evidence must be a mapping")

    parsed = parse_evidence({TELNYX_APPROVAL_KEY: evidence})
    if parsed is None:
        raise ValueError("invalid telnyx approval evidence")

    current = getattr(record, "carrier_refs", None)
    if current is None:
        new_refs: dict[str, Any] = {}
    elif isinstance(current, Mapping):
        new_refs = dict(current)
    else:
        raise TypeError("record.carrier_refs must be a mapping or None")

    new_refs[TELNYX_APPROVAL_KEY] = parsed.as_dict()
    record.carrier_refs = new_refs
    return record


def evaluate_evidence(
    carrier_refs: object,
    *,
    carrier_id: object,
    now: object,
    max_age_seconds: object,
) -> tuple[bool, str]:
    """Fail-closed approval check returning ``(is_approved, reason)``.

    Checks are applied in order: missing/malformed, bad expected carrier id or
    mismatched evidence id, invalid ``now``/``max_age_seconds``, future
    timestamp, stale, revoked, approved. ``max_age_seconds`` must be a positive
    ``int`` (bools and floats are rejected); the tighter 300..2592000 policy is
    supplied by configuration.
    """
    if not isinstance(carrier_refs, Mapping) or TELNYX_APPROVAL_KEY not in carrier_refs:
        return (False, REASON_MISSING)

    evidence = parse_evidence(carrier_refs)
    if evidence is None:
        return (False, REASON_MALFORMED)

    if not isinstance(carrier_id, str) or not carrier_id.strip():
        return (False, REASON_INVALID_CARRIER_ID)
    if evidence.carrier_id != carrier_id.strip():
        return (False, REASON_CARRIER_MISMATCH)

    if not _is_aware(now):
        return (False, REASON_INVALID_NOW)

    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int):
        return (False, REASON_INVALID_MAX_AGE)
    if max_age_seconds <= 0:
        return (False, REASON_INVALID_MAX_AGE)

    checked = _parse_utc(evidence.checked_at)
    if checked is None:
        return (False, REASON_MALFORMED)

    reference = now.astimezone(timezone.utc)
    if checked > reference:
        return (False, REASON_FUTURE)
    if (reference - checked).total_seconds() > max_age_seconds:
        return (False, REASON_STALE)
    if evidence.state == STATE_REVOKED:
        return (False, REASON_REVOKED)
    return (True, REASON_APPROVED)
