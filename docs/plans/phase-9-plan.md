# Phase 9 — AI voice agent v2: tools, warm handoff, grounding, voicemail drop

## What LiveKit changed here
Warm handoff was the hard problem this phase existed for (SIP REFER strips context). On
D17's topology it is a **room join**: the human's softphone enters the SAME room the AI and
caller are in; the AI introduces the human and leaves. Context travels as (a) the live
transcript already on the call detail, and (b) a handoff summary in the ring event. No
bridge, no REFER, nothing stripped.

## Deliverables

### 1. Tools (livekit-agents function tools on the Agent)
- `lookup_contact` → GET /api/v1/agent/contact/{e164} (name, tags, last conversation
  summary from message threads) — worker-auth seam, org from the call row.
- `book_appointment(when_iso, notes)` → POST /api/v1/agent/appointments → row in the new
  `appointments` table (migration 0009) + `appointment.booked` event on the org bus.
  Naive-datetime strings accepted verbatim from the LLM, validated ISO-ish, stored with
  the raw text preserved (the LLM's "tomorrow at 3" normalization is NOT trusted; the
  console shows both raw and parsed).
- `search_knowledge(query)` → GET /api/v1/agent/kb/search — top-k chunks. v1 retrieval
  is HONESTLY keyword-based (SQL ILIKE + simple scoring over `kb_chunks`); no vector
  store dependency. pgvector is the named upgrade path, not smuggled in now.
- `transfer_to_human(reason)` → POST /api/v1/agent/handoff → publishes `call.handoff`
  (room, reason, last-6-transcript-lines summary) to the org bus; every softphone shows
  it as a priority ring. The agent KEEPS TALKING ("let me get someone for you") until a
  human participant joins the room, then says the intro line and shuts down.
- `end_call(reason)` → polite goodbye + shutdown (+ report reason in the summary log).

### 2. Knowledge base
`kb_documents` (org, title, source) + `kb_chunks` (doc, seq, text) — migration 0009.
Console: paste/upload text on the AI Agent page; backend chunks (~1000 chars, sentence
boundaries).

### 3. Voicemail drop (outbound)
LiveKit SIP has no carrier AMD; detection is the agent's job:
- `agents/beep_detector.py` — pure Goertzel tone detector (900–1100 Hz, >250 ms
  sustained) + "long outgoing message" heuristic (uninterrupted far-end speech > N s
  while our side is silent). Unit-testable offline with synthesized tones.
- On detected voicemail: wait for the beep, wait 300 ms AFTER the beep (the no-clipping
  gate), speak the profile's `voicemail_message`, hang up. Recorded verdict lands in
  `leg.amd_result` via a new machine seam (`POST /api/v1/agent/amd`) so P11's dialer
  reads one field regardless of which layer detected the machine.

### 4. Console
Appointments list (org-wide) + handoff ring treatment in the softphone dock (distinct
style + reason + summary shown) + KB editor.

## Schema (Fable authors — migration 0009)
appointments(id, org_id, call_id?, contact_e164, raw_when, scheduled_for?, notes, status
booked|canceled|done, created_by 'ai'|user-id) ·
kb_documents(id, org_id, title, source, created…) · kb_chunks(id, org_id, document_id,
seq, text) · agent_profiles + `voicemail_message` column.

## Gate (VPS, live)
Agent books an appointment via tool call; warm-transfers to a human who sees reason +
summary and lands in the same room; voicemail drop lands with the message start intact
(replay-harness variant: synthesized greeting + beep WAV, assert our message's first
250 ms energy present in what was sent).

## Forbidden to implementers
models/**, migrations/**, config.py, providers/**, voice_plane/livekit_api.py, deploy/**.
