"""SMS segment estimation.

Bandwidth **bills and rate-limits by segment, not by message** — a 2-segment message burns
2 units of a 1 MPS budget. P11's pacing and P13's metering both need this populated from
the very first message, so it lives here from P1 rather than being backfilled.

This is an estimate. The carrier's own ``segmentCount`` from a DLR is the truth and
overwrites it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# GSM 03.38 basic character set.
GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

# Extension table: each of these costs TWO septets (an escape byte plus the char).
GSM7_EXTENDED = frozenset("€[]{}|^~\\")

GSM7_SINGLE = 160
GSM7_MULTI = 153
UCS2_SINGLE = 70
UCS2_MULTI = 67


@dataclass(frozen=True)
class SegmentEstimate:
    encoding: Literal["gsm7", "ucs2"]
    segments: int
    units: int  # septets for gsm7, UTF-16 code units for ucs2


def _is_gsm7(text: str) -> bool:
    return all(ch in GSM7_BASIC or ch in GSM7_EXTENDED for ch in text)


def estimate(text: str) -> SegmentEstimate:
    """Segments the carrier will bill for ``text``.

    One non-GSM character flips the ENTIRE message to UCS-2 (per spec) — a single emoji
    takes a 160-character message to 70.
    """
    if not text:
        return SegmentEstimate("gsm7", 1, 0)

    if _is_gsm7(text):
        units = sum(2 if ch in GSM7_EXTENDED else 1 for ch in text)
        if units <= GSM7_SINGLE:
            return SegmentEstimate("gsm7", 1, units)
        return SegmentEstimate("gsm7", -(-units // GSM7_MULTI), units)

    # UCS-2 counts UTF-16 code units, so astral chars (most emoji) cost 2.
    units = sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)
    if units <= UCS2_SINGLE:
        return SegmentEstimate("ucs2", 1, units)
    return SegmentEstimate("ucs2", -(-units // UCS2_MULTI), units)
