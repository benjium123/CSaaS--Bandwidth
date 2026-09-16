"""One failure vocabulary for every carrier: spam_blocked | carrier_rejected |
invalid_destination | opted_out | unknown (models/messaging_health.FAILURE_CLASSES).
Anything unlisted is "unknown" and COUNTED there - never guessed into a nicer bucket."""

from __future__ import annotations

#: Bandwidth codes are copied from the repo's vetted private mapping in
#: app/services/messaging_errors.py `_CODE_REASONS` - do not import that private dict.
#: INVALID/LANDLINE reasons -> invalid_destination, OPTED_OUT -> opted_out,
#: SPAM_BLOCKED -> spam_blocked, OVER_LIMIT and OUTAGE -> carrier_rejected.
_BANDWIDTH = {
    # invalid_destination / landline
    "4700": "invalid_destination",
    "4701": "invalid_destination",
    "4702": "invalid_destination",
    "4711": "invalid_destination",
    "4720": "invalid_destination",
    "4721": "invalid_destination",
    # opted_out
    "4432": "opted_out",
    "4434": "opted_out",
    "5106": "opted_out",
    # spam_blocked
    "4750": "spam_blocked",
    "4751": "spam_blocked",
    "4752": "spam_blocked",
    "4753": "spam_blocked",
    "4754": "spam_blocked",
    "4770": "spam_blocked",
    "4771": "spam_blocked",
    "4772": "spam_blocked",
    "4773": "spam_blocked",
    "4774": "spam_blocked",
    "4775": "spam_blocked",
    # over_limit / outage -> carrier_rejected
    "4405": "carrier_rejected",
    "4406": "carrier_rejected",
    "4407": "carrier_rejected",
    "5100": "carrier_rejected",
    "4401": "carrier_rejected",
    "4403": "carrier_rejected",
    "5000": "carrier_rejected",
    "5001": "carrier_rejected",
}

#: from carrier docs, unverified against live traffic
_TELNYX = {
    "40002": "spam_blocked",
    "40003": "spam_blocked",
    "40015": "spam_blocked",
    "40001": "invalid_destination",
    "40010": "invalid_destination",
    "40012": "invalid_destination",
    "40013": "invalid_destination",
    "40310": "invalid_destination",
    "40300": "opted_out",
    "40004": "carrier_rejected",
    "40005": "carrier_rejected",
    "40006": "carrier_rejected",
    "40008": "carrier_rejected",
    "40011": "carrier_rejected",
    "40014": "carrier_rejected",
    "40016": "carrier_rejected",
    "40018": "carrier_rejected",
    "40020": "carrier_rejected",
}

#: Twilio and Signalwire share this code space.
#: From carrier docs, unverified against live traffic.
_TWILIO_SIGNALWIRE = {
    "30004": "spam_blocked",
    "30007": "spam_blocked",
    "30450": "spam_blocked",
    "30005": "invalid_destination",
    "30006": "invalid_destination",
    "21614": "invalid_destination",
    "21610": "opted_out",
    "30003": "carrier_rejected",
    "30008": "carrier_rejected",
    "30034": "carrier_rejected",
}

#: from carrier docs, unverified against live traffic
_PLIVO = {
    "30": "spam_blocked",
    "451": "spam_blocked",
    "452": "spam_blocked",
    "200": "opted_out",
    "40": "invalid_destination",
    "50": "invalid_destination",
    "70": "invalid_destination",
    "100": "invalid_destination",
    "110": "invalid_destination",
    "20": "carrier_rejected",
    "90": "carrier_rejected",
    "300": "carrier_rejected",
    "420": "carrier_rejected",
}

_TABLE_SOURCES: tuple[dict[str, str], ...] = (
    _BANDWIDTH,
    _TELNYX,
    _TWILIO_SIGNALWIRE,
    _PLIVO,
)

_TABLES = {
    "bandwidth": _BANDWIDTH,
    "telnyx": _TELNYX,
    "twilio": _TWILIO_SIGNALWIRE,
    "signalwire": _TWILIO_SIGNALWIRE,
    "plivo": _PLIVO,
}

#: `messages.error_code` sometimes holds a CATEGORY string instead of a numeric carrier code
#: (see the comment in messaging_errors.public_reason).
_CATEGORY_CLASSES = {
    "rate_limited": "carrier_rejected",
    "carrier_transient": "carrier_rejected",
    "carrier_unreachable": "carrier_rejected",
    "invalid_request": "invalid_destination",
    "unregistered": "carrier_rejected",
}


def _classify_unknown_carrier(code: str) -> str:
    """Look the numeric code up in all carrier tables; only a unique match wins."""
    matches = [table[code] for table in _TABLE_SOURCES if code in table]
    if len(matches) == 1:
        return matches[0]
    return "unknown"


def classify(carrier: str | None, error_code: str | None) -> str:
    """Return the canonical failure class for a carrier error.

    Never raises. None/blank is unknown; an unlisted code for a known carrier is also
    unknown rather than guessed.
    """
    if error_code is None:
        return "unknown"
    code = error_code.strip()
    if not code:
        return "unknown"

    if code in _CATEGORY_CLASSES:
        return _CATEGORY_CLASSES[code]

    if not carrier or not carrier.strip():
        return _classify_unknown_carrier(code)

    table = _TABLES.get(carrier.strip().lower())
    if table is None:
        # A carrier name we have no table for (or the literal "unknown" the rollup uses)
        # is treated like no carrier at all: a code that is unambiguous across every
        # table still buckets, anything else stays unknown.
        return _classify_unknown_carrier(code)
    return table.get(code, "unknown")
