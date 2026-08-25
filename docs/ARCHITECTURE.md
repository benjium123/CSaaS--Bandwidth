# Architecture & Settled Decisions

> These are **decisions**, not options. Each has a reason and a named condition under which
> it may be revisited. Do not relitigate without hitting the stated condition.
> Evidence lives in `docs/research/`.

---

## D1 — SIP or WebRTC? **Neither, exactly. Hybrid, split by endpoint type.**

The question is a false binary. SIP is signaling; WebRTC is a browser media stack; both
carry audio over RTP/UDP. The carrier WebSocket media stream is a third thing entirely —
base64 codec frames in JSON over **TCP**.

**The measurement that settles it:** transport is **45–200 ms of a 450–1,800 ms** AI
voice-to-voice budget — **3–15%**. Endpointing + STT + LLM + TTS are the other **85–97%**,
and endpointing alone (300–800 ms) is a bigger lever than the entire transport layer.

> **Building our own SIP stack to shave AI latency is not justified. The win from owning
> SIP is operational — warm transfer, conferencing, supervisor whisper, multi-carrier
> failover — not latency.**

### The decision

| Endpoint | Transport | Why |
|---|---|---|
| **AI voice agent** (server process) | **Bandwidth `<StartStream mode="bidirectional">` over WSS** | Confirmed bidirectional with a `clear` barge-in primitive. A server process does not need a browser's AEC or jitter buffer. No SIP stack to secure or scale. |
| **Human agent** (browser softphone) | **WebRTC** — real UDP media path, browser-native AEC / jitter buffer / PLC | A human ear needs graceful degradation on packet loss. WebRTC gets it; TCP does not. |
| **Never** | Operator-PC ↔ VPS ↔ carrier audio chains over TCP | Our own production measurement: **300–650 ms/sec of dead air** on a long-haul TCP audio leg. Consistent with TCP RTO backoff under WAN jitter. |

### The condition that forces a change
Move to a self-owned SIP stack (FreeSWITCH/Kamailio+RTPengine, or adopt LiveKit SIP /
jambonz wholesale rather than hand-rolling) **only when one of these is true**:
1. Native SIP REFER warm transfer + supervisor listen-in/whisper + multi-party conferencing
   become first-class product requirements, **or**
2. We need multi-carrier failover at the media layer, **or**
3. Per-minute carrier WebRTC/Call-Control markup becomes material at our volume.

**Do not build it preemptively "for latency."** That is not where the latency is.

### The mandatory mitigation
The WS/TCP leg is only safe while it is **short and same-region**. Every vendor solving this
problem (Daily, Telnyx) says the same thing first: **co-locate with the carrier's media PoP.**

> **PHASE 7 GATE:** measure round-trip and packet-loss behaviour between our VPS and
> Bandwidth's media servers before building the AI agent on top. If the leg is long-haul,
> move the media-consuming process to a region-pinned host — do not proceed and hope.

---

## D2 — Carrier abstraction: model it **Telnyx-shaped**, adapt Bandwidth down

The two carriers have fundamentally different control-flow models:

- **Bandwidth = document-return.** Your webhook handler must *synchronously return a BXML
  document* saying what to do next. Control is welded to the HTTP response of the callback.
- **Telnyx = imperative, out-of-band.** Ack the webhook with 200; fire
  `POST /v2/calls/{id}/actions/{command}` whenever you like, from any process.

**Decision: the internal interface is `event in → command out, async` (the Telnyx shape).**

Rationale: that model serializes *down* onto Bandwidth — the adapter collects the emitted
commands and renders them as one BXML response. The reverse does not work: a
document-return abstraction forces you to hold HTTP responses open on Telnyx.

**Accepted consequence:** the **Bandwidth adapter is the constrained one.** It can only
express commands that fit "what to do in reply to this one event." Anything genuinely
mid-stream (a barge-in `clear`) either goes over the media WebSocket or needs a second
round-trip via the update-call/`Redirect` path. Design for that, don't be surprised by it.

Also note the two carriers differ on webhook auth: **Bandwidth = HTTP Basic on the callback,
Telnyx = Ed25519 signature.** The adapter carries two verify functions. There is no shared one.

---

## D3 — Stack

| Layer | Choice | Note |
|---|---|---|
| Backend | **Python 3.12 + FastAPI** (async) | Async + native WebSockets is the requirement; matches existing team fluency |
| Scaffold | **fork `fastapi/full-stack-fastapi-template`** (MIT, 45k★) | Exact stack match. **Ships only JWT + superuser/user binary — no multi-tenancy, no RBAC. We build those.** |
| DB | **Postgres 16** | |
| Cache / queue | **Redis** (separate logical DBs for cache vs queue) | |
| Object store | **S3-compatible** (MinIO local, R2/B2/S3 prod) | recordings, MMS media, voicemail, transcripts |
| Migrations | **Alembic** | |
| Frontend | **React + Vite + TypeScript + Tailwind + shadcn/ui** | |
| AI voice | **Pipecat** (BSD-2) + **`Bandwidth/pipecat-bandwidth`** (BSD-2, first-party) | see D4 |
| STT | **Deepgram Nova-3** primary, AssemblyAI fallback | bake off against real recorded calls before locking |
| TTS | **ElevenLabs Flash v2.5** (`ulaw_8000` output) primary, **Cartesia Sonic** as the speed upgrade | |
| LLM | pluggable per surface — voice / SMS / classifier configured independently | see `.env` |
| Numbers | **python-phonenumbers** | |
| Deploy | Docker Compose + nginx on VPS | |

---

## D4 — AI pipeline: **cascaded, not speech-to-speech**

STT → LLM → TTS, not OpenAI Realtime / Gemini Live, because we need:
1. **An explicit text transcript at every hop** — for compliance/audit and for feeding
   downstream classification, scoring and analytics.
2. **A real LLM step we can prompt, tool and function-call** like any text agent, with RAG
   and business rules injected mid-turn.
3. **Component-level swappability** on price and latency.

A well-engineered cascade now matches or beats S2S on voice-to-voice latency. S2S is the
better choice only for simple receptionist/FAQ bots with no transcript or business-logic
requirement. Revisit only if we ship such a bot as a separate product.

**Latency targets: p50 ≤ 700 ms, p95 ≤ 1100 ms** voice-to-voice.
Sub-500 ms is a stretch goal requiring co-located STT/LLM/TTS and Groq-class inference.
**Tune endpointing first** — it is the single biggest controllable component.

---

## D5 — Audio-pipeline law (carried forward, non-negotiable)

> **A queued frame may be shed ONLY if that frame is itself silent (per-frame peak) — never
> because silence is arriving.** Shedding on "silence is arriving" deletes sentence tails.

> **`rt = 1.0` does NOT prove low latency.** It hides standing queue depth.

**Every audio change must pass a conversation-replay test before deploy.** This test is a
Phase-7 deliverable and it gates every subsequent audio commit.

Barge-in must do all five steps or it is broken:
1. VAD detects caller speech over threshold during TTS playback
2. stop local playback
3. **cancel the in-flight TTS request**
4. cancel/truncate the current LLM generation
5. **send `clear` to the carrier** to flush audio already buffered downstream

Known failure modes to write tests against: self-echo re-triggering VAD; false-positive VAD
on background noise; cut-off sentence tails from a too-low threshold; the buffer-depth race
where the caller still hears ~half a second of stale audio after a successful `clear`.

---

## D6 — Webhook ingestion: idempotent and state-based, always

Both carriers retry aggressively and **neither guarantees ordering**:
- **Bandwidth retries any non-2xx for 24 hours, unordered, in parallel with in-flight retries.**
- **Telnyx is at-least-once and may deliver the same event to both the primary and the
  failover URL.**

Therefore, non-negotiable from Phase 1:
- Dedupe on the provider's event/message ID, persisted.
- Handlers are **state-machine transitions**, never "increment" or "append" side effects.
- 2xx fast (< 2 s), do the work asynchronously. Bandwidth's own callback timeouts are as
  low as 1–25 s, and a slow handler is treated as a failure → retry → duplicate BXML.
- **A "call" is N legs.** Callbacks fire per leg. Model legs explicitly or transfers,
  conferences and AMD will break the state machine.

---

## D7 — Compliance is a first-class module, not a feature flag

There is **no OSS TCPA/DNC library in any language** and **no portable 10DLC automation
library**. Both are confirmed structural gaps. We build them.

Hard requirements, enforced centrally in the send path — not in each caller:
- **STOP suppresses across the whole number pool**, not per-number. Per-number opt-out lists
  are a bug.
- **Quiet hours resolve in the recipient's timezone**, not the server's. DST breaks naive
  implementations twice a year.
- **Recording consent: if either party is in a two-party-consent state, the whole call needs
  consent. Area code ≠ physical location.**
- Consent ledger is append-only and auditable.

---

## D8 — What we are NOT doing

| Not doing | Why |
|---|---|
| Forking Chatwoot | Rails + Vue. Harvest the data model and thread UX instead. |
| Forking Dograh wholesale | Voice-only, no SMS/contacts/inbox. Forking makes our core someone else's AI-voice product with SMS bolted on. Harvest its workflow builder. |
| Running jambonz | v10+ core requires a paid license keyed to your DNS domain. Keep as the SIP fallback only, and verify the license of the exact tag first. |
| Anything AGPL/Commons-Clause in the core | erxes is GPLv3+Commons Clause and explicitly bans this business model. Lago/FreeScout/Zammad are AGPL — sidecar only, never forked. |
| Headless-browser webphone for humans | Measured: headless Chrome renders audio at **0.68× under call load**; headless-under-Xvfb on a VPS has no hardware clock at all. Fallback seats and AI only. |
| RCS | Bandwidth A2P RCS at scale is "coming soon". Not production-ready. |
| Vocode | Dead — last release Jun 2024, last push Nov 2024. |

---

## Open risks to resolve early

| # | Risk | Resolve by |
|---|---|---|
| R1 | **Bandwidth account access.** Historically sales-gated with no public pricing. "Bandwidth Build" (2026-06-23) is the self-serve answer, but whether the trial scales to production without a sales contract is unverified. | **Phase 0 — blocker.** Confirm before anything else. |
| R2 | VPS ↔ Bandwidth media PoP is long-haul → TCP dead air | Phase 7 gate: measure before building |
| R3 | Deepgram Nova-3 real WER on our 8 kHz caller audio (marketed 5.26% vs 12–25% independent) | Phase 8 bake-off vs AssemblyAI |
| R4 | Two live Bandwidth doc generations; legacy `v2.dev.` / `old.dev.` / `catapult` endpoints look authoritative in search | Standing rule: only `dev.bandwidth.com/docs/...` |
| R5 | 10DLC brand/campaign vetting + TFV take days–weeks; throughput is now Trust-Score-driven, not a flat 1 MPS | Start registration in **Phase 4**, long before we need throughput |
| R6 | Bandwidth `<StartStream>` frame size (ms/frame) undocumented; DTMF does not arrive over the media WS | Phase 7 — measure frames, wire DTMF via the voice webhook |
| R7 | Bandwidth conference: max 20 participants, 24 h, and **you cannot `StartStream` a conference room** — the AI must stream on the leg *before* it joins | Phase 12 design constraint |
