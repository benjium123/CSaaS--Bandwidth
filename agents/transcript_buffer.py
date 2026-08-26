from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Segment:
    role: str
    text: str
    at_ms: int


class TranscriptBuffer:
    def __init__(
        self,
        flush_after: int = 5,
        max_age_seconds: float = 3.0,
        max_buffered: int = 2000,
    ) -> None:
        self.flush_after = flush_after
        self.max_age_seconds = max_age_seconds
        self.max_buffered = max_buffered
        self._segments: list[Segment] = []
        self._oldest_buffered_since: float | None = None
        #: Last segment DRAINED (i.e. already posted or in flight to the backend) per
        #: role - an anchor so a same-role, near-duplicate segment arriving just after
        #: a flush boundary can still be recognized as a growing STT final instead of
        #: being appended as a brand-new, wrongly-duplicated segment (F8).
        self._last_drained: dict[str, Segment] = {}
        #: Count of oldest-segment drops caused by hitting max_buffered - surfaced so
        #: tests/ops can tell the cap actually engaged rather than just trusting silence.
        self.dropped_count = 0

    def _append(self, segment: Segment, now: float) -> None:
        self._segments.append(segment)
        if self._oldest_buffered_since is None:
            self._oldest_buffered_since = now
        if len(self._segments) > self.max_buffered:
            dropped = self._segments.pop(0)
            self.dropped_count += 1
            logger.warning(
                "transcript buffer over max_buffered=%d; dropping oldest segment "
                "role=%s at_ms=%d (dropped_count=%d)",
                self.max_buffered,
                dropped.role,
                dropped.at_ms,
                self.dropped_count,
            )

    def add(self, role: str, text: str, at_ms: int, now: float) -> None:
        text = text.strip()
        if not text:
            return

        if self._segments:
            last = self._segments[-1]
            if (
                last.role == role
                and abs(last.at_ms - at_ms) <= 1500
                and text.startswith(last.text)
            ):
                last.text = text
                return
        else:
            anchor = self._last_drained.get(role)
            if (
                anchor is not None
                and abs(anchor.at_ms - at_ms) <= 1500
                and text.startswith(anchor.text)
            ):
                if text == anchor.text:
                    # Exact duplicate of what was already drained: the STT re-emitted
                    # the same final just after the flush boundary. Drop it entirely -
                    # the server's (call_id, role, at_ms) unique constraint would dedupe
                    # it anyway, so nothing is lost by not sending it.
                    return
                # It grew past what was already drained. We cannot reuse the anchor's
                # at_ms unmodified: the server dedupes on (call_id, role, at_ms), so an
                # insert at the SAME at_ms would be silently skipped as a duplicate and
                # the grown tail would be lost. Send the full text one ms after the
                # anchor's at_ms instead, landing it as a small near-duplicate row right
                # next to the original - a small near-duplicate beats a lost tail.
                self._append(
                    Segment(role=role, text=text, at_ms=anchor.at_ms + 1), now
                )
                return

        self._append(Segment(role=role, text=text, at_ms=at_ms), now)

    def due(self, now: float) -> bool:
        if len(self._segments) >= self.flush_after:
            return True
        return bool(self._segments and self._oldest_buffered_since is not None and now - self._oldest_buffered_since >= self.max_age_seconds)

    def drain(self) -> list[Segment]:
        segments = self._segments
        self._segments = []
        self._oldest_buffered_since = None
        for segment in segments:
            self._last_drained[segment.role] = segment
        return segments


def assemble_instructions(
    system_prompt: str,
    org_name: str,
    contact_e164: str,
    direction: str,
    extra_rules: list[str],
) -> str:
    extra_rules = extra_rules or []
    direction_text = (
        "You called them." if direction.lower() == "outbound" else "They called you."
    )

    lines = [
        system_prompt,
        "",
        "Platform instructions:",
        f"Direction: {direction_text}",
    ]
    if org_name:
        lines.append(f"Organization: {org_name}")
    lines.append(f"Contact number: {contact_e164 or 'unknown'}")
    lines.append("Hard rules:")
    lines.extend(
        [
            "- Be concise; use one or two sentences per turn.",
            "- Never invent facts about the business.",
            "- If the caller asks for a human, say you will transfer them.",
            "- If the caller asks to stop calling, apologize and end the call.",
        ]
    )
    for rule in extra_rules:
        rule_text = str(rule).strip()
        if rule_text:
            lines.append(f"- {rule_text}")

    return "\n".join(lines)
