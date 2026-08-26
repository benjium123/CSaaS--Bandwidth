from __future__ import annotations

import pytest

from app.providers.segments import estimate


@pytest.mark.parametrize(
    "length,expected",
    [(1, 1), (159, 1), (160, 1), (161, 2), (306, 2), (307, 3)],
)
def test_gsm7_boundaries(length, expected):
    assert estimate("a" * length).segments == expected
    assert estimate("a" * length).encoding == "gsm7"


@pytest.mark.parametrize("length,expected", [(69, 1), (70, 1), (71, 2), (134, 2), (135, 3)])
def test_ucs2_boundaries(length, expected):
    # 'é' is GSM-7, but 'ĕ' is not — one non-GSM char flips the WHOLE message to UCS-2.
    text = "ĕ" * length
    est = estimate(text)
    assert est.encoding == "ucs2"
    assert est.segments == expected


def test_one_non_gsm_char_flips_entire_message():
    """The classic billing surprise: a single emoji takes 160 chars from 1 segment to 3."""
    assert estimate("a" * 160).segments == 1
    assert estimate("a" * 159 + "😀").encoding == "ucs2"
    assert estimate("a" * 159 + "😀").segments == 3


def test_extension_chars_cost_two_septets():
    assert estimate("€" * 80).segments == 1  # 160 septets exactly
    assert estimate("€" * 81).segments == 2  # 162 septets


def test_empty_text_is_one_segment():
    assert estimate("").segments == 1
