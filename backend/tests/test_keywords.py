"""Keyword classification — including the regression test for a real production incident.

A previous system matched STOP *inside* message bodies. Marketing footers say "Reply STOP
to unsubscribe", so quoting or forwarding one of our own messages silently DNC'd real
buyers. These tests exist so that cannot come back.
"""

from __future__ import annotations

import pytest

from app.compliance.keywords import classify_keyword, normalize


@pytest.mark.parametrize(
    "text,kind",
    [
        ("STOP", "opt_out"),
        ("stop", "opt_out"),
        (" Stop. ", "opt_out"),
        ("STOPALL", "opt_out"),
        ("Unsubscribe!", "opt_out"),
        ("cancel", "opt_out"),
        ("END", "opt_out"),
        ("quit", "opt_out"),
        ("START", "opt_in"),
        ("yes", "opt_in"),
        ("UNSTOP", "opt_in"),
        ("HELP", "help"),
        ("info", "help"),
    ],
)
def test_exact_keywords_are_hits(text, kind):
    hit = classify_keyword(text)
    assert hit is not None, f"{text!r} should be a keyword"
    assert hit.kind == kind


@pytest.mark.parametrize(
    "text",
    [
        # THE INCIDENT: our own footer, quoted back at us by a real customer.
        "Reply STOP to unsubscribe",
        "Text STOP to opt out. Msg & data rates may apply.",
        "123 Main St - reply STOP to unsubscribe",
        # Conversational refusals: a human/AI concern, not a carrier keyword.
        "please stop texting me",
        "I want to stop",
        "stop it",
        "Can you stop calling",
        # Ordinary replies that contain a keyword as a word.
        "Yes I'm interested in the house",
        "yes please",
        "help me understand the price",
        "I need help with the offer",
        "no",
        "",
        None,
    ],
)
def test_non_keywords_are_never_hits(text):
    assert classify_keyword(text) is None, (
        f"{text!r} must NOT be treated as a keyword - this is the footer-regex incident"
    )


def test_normalize_only_strips_trailing_punctuation():
    assert normalize("  STOP!!  ") == "stop"
    assert normalize("stop.") == "stop"
    # Interior punctuation is NOT stripped - doing so would drift back toward substring
    # matching.
    assert normalize("stop,now") == "stop,now"
    assert classify_keyword("stop,now") is None


def test_multiword_is_never_a_keyword():
    """The whole safety property in one assertion."""
    for word in ("stop", "start", "help", "unsubscribe"):
        assert classify_keyword(word) is not None
        assert classify_keyword(f"{word} please") is None
        assert classify_keyword(f"please {word}") is None
