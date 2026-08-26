"""The transcript buffer feeds an at-least-once HTTP ingest; what matters is that
nothing is lost, nothing is duplicated by coalescing, and time comes from the caller."""

from __future__ import annotations

from agents.transcript_buffer import TranscriptBuffer, assemble_instructions


def test_growing_stt_final_replaces_instead_of_duplicating() -> None:
    """Streaming STT emits finals that grow ("Hello" -> "Hello there"). Appending both
    would transcribe the caller twice."""
    b = TranscriptBuffer(flush_after=10)
    b.add("user", "Hello", 100, now=0.0)
    b.add("user", "Hello there", 900, now=0.5)
    segs = b.drain()
    assert [s.text for s in segs] == ["Hello there"]


def test_role_change_breaks_coalescing() -> None:
    b = TranscriptBuffer(flush_after=10)
    b.add("user", "Hi", 100, now=0.0)
    b.add("agent", "Hi yourself", 400, now=0.1)
    assert [s.role for s in b.drain()] == ["user", "agent"]


def test_due_on_count_and_on_age() -> None:
    b = TranscriptBuffer(flush_after=2, max_age_seconds=3.0)
    b.add("user", "one", 0, now=0.0)
    assert not b.due(now=0.1)
    b.add("agent", "two", 500, now=0.2)
    assert b.due(now=0.2), "count threshold"

    b2 = TranscriptBuffer(flush_after=99, max_age_seconds=3.0)
    b2.add("user", "old", 0, now=10.0)
    assert not b2.due(now=11.0)
    assert b2.due(now=13.5), "age threshold"


def test_drain_clears_and_empty_is_safe() -> None:
    b = TranscriptBuffer()
    assert b.drain() == []
    b.add("user", "x", 0, now=0.0)
    assert len(b.drain()) == 1
    assert b.drain() == []


def test_blank_text_is_dropped() -> None:
    b = TranscriptBuffer()
    b.add("user", "   ", 0, now=0.0)
    assert b.drain() == []


def test_max_buffered_drops_oldest_and_counts_drops() -> None:
    b = TranscriptBuffer(flush_after=9999, max_age_seconds=9999.0, max_buffered=3)
    for i in range(5):
        b.add("user", f"seg-{i}", i * 100, now=float(i))

    segs = b.drain()
    # Only the newest 3 survive; the 2 oldest were dropped as the cap was crossed.
    assert [s.text for s in segs] == ["seg-2", "seg-3", "seg-4"]
    assert b.dropped_count == 2


def test_growing_final_across_a_flush_boundary_lands_adjacent_not_lost() -> None:
    """F8: STT emits "Hello" then flush drains it, THEN the same STT stream emits the
    grown final "Hello there" a moment later. Appending it as a brand-new segment at
    its own at_ms is fine UNLESS it collides with the anchor's at_ms (server dedupes on
    (call_id, role, at_ms)) - and re-sending the exact same text again would just be a
    wasted duplicate post."""
    b = TranscriptBuffer(flush_after=1, max_age_seconds=9999.0)
    b.add("user", "Hello", 100, now=0.0)
    drained = b.drain()
    assert [s.text for s in drained] == ["Hello"]

    # Exact repeat of the anchor shortly after the flush boundary: dropped entirely.
    b.add("user", "Hello", 300, now=0.3)
    assert b.drain() == []

    # Grown past the anchor, still within the coalescing window: sent as a new
    # segment, timestamped one ms after the anchor so it cannot collide on
    # (role, at_ms) with the already-drained/posted row.
    b.add("user", "Hello there", 900, now=0.9)
    grown = b.drain()
    assert [s.text for s in grown] == ["Hello there"]
    assert grown[0].at_ms == 101

    # Anchor is per-role: an "agent" segment right after must NOT be treated as a
    # continuation of the "user" anchor above.
    b.add("agent", "Hello there, how can I help?", 950, now=0.95)
    agent_drained = b.drain()
    assert [s.text for s in agent_drained] == ["Hello there, how can I help?"]


def test_assemble_instructions_carries_the_hard_rules() -> None:
    p = assemble_instructions(
        "Be the Sabine assistant.", "Sabine", "+12145550100", "outbound", ["never quote prices"]
    )
    assert "Sabine assistant" in p
    assert "You called them" in p
    assert "never quote prices" in p
    assert "transfer" in p.lower(), "the ask-for-a-human rule must always be present"
