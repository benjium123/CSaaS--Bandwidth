"""P41: one failure vocabulary across every carrier.

Pure functions, no DB. The point of these cases is that a code is either in a table or it
is "unknown" - it is never guessed into a nicer bucket because it looks like another
carrier's code.
"""

from __future__ import annotations

import pytest

from app.providers.failure_classes import classify

KNOWN_CASES = [
    # Bandwidth (derived from the repo's vetted _CODE_REASONS mapping)
    ("bandwidth", "4700", "invalid_destination"),
    ("bandwidth", "4701", "invalid_destination"),
    ("bandwidth", "4702", "invalid_destination"),
    ("bandwidth", "4711", "invalid_destination"),
    ("bandwidth", "4720", "invalid_destination"),
    ("bandwidth", "4721", "invalid_destination"),
    ("bandwidth", "4432", "opted_out"),
    ("bandwidth", "4434", "opted_out"),
    ("bandwidth", "5106", "opted_out"),
    ("bandwidth", "4750", "spam_blocked"),
    ("bandwidth", "4751", "spam_blocked"),
    ("bandwidth", "4752", "spam_blocked"),
    ("bandwidth", "4753", "spam_blocked"),
    ("bandwidth", "4754", "spam_blocked"),
    ("bandwidth", "4770", "spam_blocked"),
    ("bandwidth", "4771", "spam_blocked"),
    ("bandwidth", "4772", "spam_blocked"),
    ("bandwidth", "4773", "spam_blocked"),
    ("bandwidth", "4774", "spam_blocked"),
    ("bandwidth", "4775", "spam_blocked"),
    ("bandwidth", "4405", "carrier_rejected"),
    ("bandwidth", "4406", "carrier_rejected"),
    ("bandwidth", "4407", "carrier_rejected"),
    ("bandwidth", "5100", "carrier_rejected"),
    ("bandwidth", "4401", "carrier_rejected"),
    ("bandwidth", "4403", "carrier_rejected"),
    ("bandwidth", "5000", "carrier_rejected"),
    ("bandwidth", "5001", "carrier_rejected"),
    # Telnyx
    ("telnyx", "40002", "spam_blocked"),
    ("telnyx", "40003", "spam_blocked"),
    ("telnyx", "40015", "spam_blocked"),
    ("telnyx", "40001", "invalid_destination"),
    ("telnyx", "40010", "invalid_destination"),
    ("telnyx", "40012", "invalid_destination"),
    ("telnyx", "40013", "invalid_destination"),
    ("telnyx", "40310", "invalid_destination"),
    ("telnyx", "40300", "opted_out"),
    ("telnyx", "40004", "carrier_rejected"),
    ("telnyx", "40005", "carrier_rejected"),
    ("telnyx", "40006", "carrier_rejected"),
    ("telnyx", "40008", "carrier_rejected"),
    ("telnyx", "40011", "carrier_rejected"),
    ("telnyx", "40014", "carrier_rejected"),
    ("telnyx", "40016", "carrier_rejected"),
    ("telnyx", "40018", "carrier_rejected"),
    ("telnyx", "40020", "carrier_rejected"),
    # Twilio and Signalwire share a code space
    ("twilio", "30004", "spam_blocked"),
    ("twilio", "30007", "spam_blocked"),
    ("twilio", "30450", "spam_blocked"),
    ("twilio", "30005", "invalid_destination"),
    ("twilio", "30006", "invalid_destination"),
    ("twilio", "21614", "invalid_destination"),
    ("twilio", "21610", "opted_out"),
    ("twilio", "30003", "carrier_rejected"),
    ("twilio", "30008", "carrier_rejected"),
    ("twilio", "30034", "carrier_rejected"),
    ("signalwire", "30004", "spam_blocked"),
    ("signalwire", "30005", "invalid_destination"),
    ("signalwire", "21610", "opted_out"),
    # Plivo
    ("plivo", "30", "spam_blocked"),
    ("plivo", "451", "spam_blocked"),
    ("plivo", "452", "spam_blocked"),
    ("plivo", "200", "opted_out"),
    ("plivo", "40", "invalid_destination"),
    ("plivo", "50", "invalid_destination"),
    ("plivo", "70", "invalid_destination"),
    ("plivo", "100", "invalid_destination"),
    ("plivo", "110", "invalid_destination"),
    ("plivo", "20", "carrier_rejected"),
    ("plivo", "90", "carrier_rejected"),
    ("plivo", "300", "carrier_rejected"),
    ("plivo", "420", "carrier_rejected"),
]


@pytest.mark.parametrize("carrier,code,expected", KNOWN_CASES)
def test_known_codes_map_to_bucket(carrier, code, expected):
    assert classify(carrier, code) == expected


@pytest.mark.parametrize(
    "carrier,code",
    [
        ("telnyx", "99999"),
        ("bandwidth", "1234"),
        ("plivo", "4750"),
    ],
)
def test_unlisted_code_is_unknown(carrier, code):
    # A known carrier's missing code is never borrowed from another carrier's table.
    assert classify(carrier, code) == "unknown"


@pytest.mark.parametrize(
    "carrier,code",
    [(None, None), ("telnyx", None), ("telnyx", ""), ("telnyx", "   "), (None, "")],
)
def test_none_or_blank_is_unknown(carrier, code):
    assert classify(carrier, code) == "unknown"


def test_bandwidth_spam_families_regression():
    # The 4750-4754 and 4770-4775 families are what reputation.py calls spam blocks.
    # If these ever drift, the two surfaces disagree about the same message.
    assert classify("bandwidth", "4750") == "spam_blocked"
    assert classify("bandwidth", "4770") == "spam_blocked"


@pytest.mark.parametrize(
    "code,expected",
    [
        ("rate_limited", "carrier_rejected"),
        ("carrier_transient", "carrier_rejected"),
        ("carrier_unreachable", "carrier_rejected"),
        ("invalid_request", "invalid_destination"),
        ("unregistered", "carrier_rejected"),
    ],
)
def test_category_strings_map_whatever_the_carrier(code, expected):
    # error_code sometimes holds a CATEGORY string rather than a carrier code.
    assert classify(None, code) == expected
    assert classify("telnyx", code) == expected


def test_unknown_carrier_falls_back_to_a_unique_table_match():
    assert classify(None, "40015") == "spam_blocked"
    assert classify("unknown", "40001") == "invalid_destination"


def test_code_in_two_tables_under_an_unknown_carrier_stays_unknown():
    # "30" is a Plivo spam block; no other table claims it, so it still resolves. A code
    # that two tables disagree about must not: exactly one match is the rule.
    assert classify(None, "30") == "spam_blocked"
    assert classify(None, "9999999") == "unknown"
