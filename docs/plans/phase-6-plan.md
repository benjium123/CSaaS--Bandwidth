# Phase 6 — LiveKit media plane + browser softphone

## The spike, resolved (D17)

The P6 decision spike asked: Bandwidth In-App Calling vs Telnyx WebRTC vs SIP.js. The
answer is **none of them — LiveKit for both endpoint types**, and here is the chain:

1. D14 (user directive) already commits the AI agent to LiveKit Agents.
2. D1's revisit condition 1 has FIRED: warm transfer, supervisor listen-in and AI↔human
   handoff are first-class requirements of this product (an AI caller a human can take
   over is the whole point). D1 itself says the win from owning the media layer is
   operational, and these are exactly the operations.
3. If the AI lives in LiveKit rooms and the softphone lives on a carrier WebRTC SDK, every
   AI↔human handoff crosses two media infrastructures via carrier-side transfer — context
   lost, two vendors to debug. If both are room participants, handoff/barge/whisper are
   **room operations**.
4. Carrier WebRTC SDKs also bind the softphone to ONE carrier, which the P3b fabric exists
   to avoid. A SIP trunk into LiveKit keeps carriers swappable at the trunk level.

**Fallback (named, per house rules):** if the Phase-7 media measurement fails on the VPS
(long-haul RTP, SFU starvation on the shared box) and region-pinning a media host doesn't
cure it, the softphone falls back to Telnyx WebRTC SDK and the AI stays on LiveKit
elsewhere — accepted split, documented as such. Do not fall back for any lesser reason.

## Topology

```
PSTN ↔ carrier (Telnyx SIP trunk primary; Bandwidth trunk later)
     ↔ livekit-sip (SIP participant)
     ↔ LiveKit SFU room  ←— browser softphone (livekit-client, WebRTC/UDP)
                          ←— AI agent worker (P7/P8, LiveKit Agents)
```

- One room per call. The SIP participant IS the PSTN leg.
- P5's model already fits: Call + CallLeg rows as today; a LiveKit-routed leg stores the
  SIP call ID in `provider_call_id` and `extra["room"]` names the room. The carrier column
  keeps naming the TRUNK carrier (telnyx/bandwidth) — LiveKit is infrastructure, not a
  carrier, and must never appear as one in routing.
- P5's webhook state machine stays authoritative for carrier-side truth; LiveKit webhooks
  (room started/finished, participant joined/left) enrich, never replace.

## Pre-dependencies (user-facing, wall-clock)
- Telnyx: create a SIP Connection (FQDN/credential trunk), assign a number to it for
  voice, note the SIP URI + credentials → .env (`LIVEKIT_*`, `TELNYX_SIP_*` — config.py
  additions are Fable's).
- VPS: open UDP range for RTP (LiveKit default 50000–60000) + 7880–7882; docker compose
  for `livekit`, `livekit-sip`, `redis` (infra files land in `deploy/livekit/`).
- LiveKit keys: self-generated (self-hosted), no external account needed.

## Deliverables
1. `deploy/livekit/` — compose + livekit.yaml + sip config, documented.
2. Backend (`app/voice_plane/`): access-token minting (JWT, room grants, TTL ≤ 1h,
   identity = user id), room-per-call orchestration, outbound dial = create room + SIP
   participant via LiveKit API, inbound dispatch rule → room + ring events to the org's
   agents over the existing WS/event channel; LiveKit webhook receiver (signature-verified)
   feeding P5 leg state.
3. API: POST /api/v1/softphone/token; POST /api/v1/calls gains `via: "sip_room"` path;
   ring notification event.
4. Frontend softphone (livekit-client): inbound ring UI, outbound dial pad,
   **per-call caller-ID picker** (org numbers list → trunk `from`), hold, mute, DTMF
   (SIP INFO via API), device selection, reconnection (livekit-client handles ICE restart;
   UI must surface state honestly).

## Test spec
Unit: token grants minimal (room-scoped, no admin), TTL enforced; room name derivation
stable per call; dispatch-rule payload → correct org + Call row; LiveKit webhook signature
verify + idempotent leg transitions (reuse P5 dedupe); caller-ID picker only offers active,
voice-capable org numbers (registration gate NOT applied — it is 10DLC/messaging law, not
voice law; document this).
Integration: outbound dial creates room + SIP participant (LiveKit API mocked) + P5 rows;
inbound webhook sequence rings then answers then hangs up, deriving call status correctly.
Manual gate: place + answer a real call in the browser, pick caller ID per call, ear-test —
no one-way audio, no dead air. (Requires live trunk; user-assisted.)

## Deploy
yes — VPS compose up + backend deploy. Softphone ships behind a feature flag until the
manual ear-test passes.
