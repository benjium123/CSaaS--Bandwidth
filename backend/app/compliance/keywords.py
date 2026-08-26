"""Opt-out / opt-in / help keyword classification.

**This module exists because of a real production incident.** A previous system used a
regex that looked for STOP *inside* a message body. Marketing footers say "Reply STOP to
unsubscribe" — so when legitimate buyers quoted or forwarded one of our own messages, the
regex matched and DNC'd them. Real customers were silently suppressed.

The fix is structural, not a better regex: **the entire message must equal a keyword.**
No substring matching, no regex-over-body, no "contains", ever. A multi-word message is
never a keyword, full stop. If someone writes "please stop texting me", that is a
conversational refusal for a human or the AI agent to handle (and the manual opt-out
endpoint exists for exactly that) — it is not a carrier-protocol keyword.

Pure module: no database, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Carrier/CTIA standard keyword families.
OPT_OUT_WORDS = frozenset({"stop", "stopall", "unsubscribe", "cancel", "end", "quit"})
OPT_IN_WORDS = frozenset({"start", "yes", "unstop"})
HELP_WORDS = frozenset({"help", "info"})

# Only TRAILING punctuation is forgiven. "Stop." and "STOP!" are the same intent; but
# "stop it" is not, and stripping interior punctuation would start us back down the road
# to substring matching.
_TRAILING_PUNCTUATION = ".!,;:?"

KeywordKind = Literal["opt_out", "opt_in", "help"]


@dataclass(frozen=True)
class KeywordHit:
    kind: KeywordKind
    matched: str


def normalize(text: str) -> str:
    """Trim, drop trailing punctuation, casefold. Nothing else."""
    return (text or "").strip().rstrip(_TRAILING_PUNCTUATION).strip().casefold()


def classify_keyword(text: str | None) -> KeywordHit | None:
    """Return the keyword this message IS, or None.

    Note the deliberate absence of any search: the normalized message is compared for
    EQUALITY against each set. That is the whole safety property.
    """
    if not text:
        return None

    word = normalize(text)
    if not word:
        return None

    if word in OPT_OUT_WORDS:
        return KeywordHit("opt_out", word)
    if word in OPT_IN_WORDS:
        return KeywordHit("opt_in", word)
    if word in HELP_WORDS:
        return KeywordHit("help", word)
    return None
