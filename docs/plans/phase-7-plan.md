# Phase 7 — Media plane stood up, measured, and proven with an echo agent

P7 is still **the risk phase**, but D14/D17 change what the risk is. The original plan
hand-rolled a WSS media server against Bandwidth `<StartStream>`; LiveKit now owns the
frame codec, jitter buffer, pacing and barge-in plumbing. What remains OURS to prove:

1. **The measurement gate (R2, unchanged in spirit):** the VPS is a shared, busy box.
   Before building on it: RTT/jitter/loss from VPS to the Telnyx SIP/RTP edge, AND SFU
   forward latency under synthetic load on that box. Long-haul or starved → region-pin a
   dedicated media host — do not proceed and hope. (House law: an idle probe lies; measure
   under load.)
2. **The echo agent:** a LiveKit Agents (Python) worker that joins the room and plays the
   caller's audio back. This proves the full loop PSTN → trunk → SIP bridge → SFU → agent
   → back, which is every hop P8's AI will use, with zero AI latency mixed in.
3. **The conversation-replay harness** — the artifact that gates every later audio commit
   (house audio law). Recorded caller WAVs are injected as a room participant; the
   harness asserts (a) round-trip audio intact — no dropped sentence TAILS (frame-shed
   law: a frame may be shed only if ITSELF silent), (b) rt ≈ 1.0 AND standing queue depth
   measured separately (rt alone proves nothing — D5), (c) no underruns over 5 minutes.
4. **Barge-in primitive:** echo agent variant that stops playback the instant the caller
   speaks (VAD interrupt). Latency speech-onset → playback-stop measured; this number
   gates P8's five-step barge-in chain.
5. **DTMF path:** arrives via SIP INFO / carrier webhook, NOT the media stream —
   wire LiveKit SIP DTMF events into the P5 dtmf_received flow.

## Deliverables
- `deploy/livekit/` finalized from P6 + `measure/` scripts (RTT, SFU load, results
  committed as `docs/research/p7-media-measurements.md` — numbers, not adjectives).
- `agents/` (new top-level Python package, own venv/requirements — LiveKit Agents SDK is
  heavy and must NOT enter the backend's dependency set): `echo_agent.py`,
  `replay_harness.py`, `tests/test_conversation_replay.py`.
- Backend: agent-dispatch hook — inbound call marked `ai=true` dispatches the agent worker
  to the room (LiveKit agent dispatch API); Call row gains nothing (extra["agent"] only).

## Gate (all measured, all published in the research doc)
- Echo bot works on a REAL phone call end-to-end.
- Media measurements published; either the VPS passes or the media host is region-pinned.
- Replay harness green: rt ≈ 1.0, standing queue depth reported, zero underruns / 5 min.
- Barge-in stop latency measured (< 300 ms target).

## Deploy
yes (compose + agent worker as a systemd/compose service).
