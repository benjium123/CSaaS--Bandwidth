# Phases

15 phases (P0–P14). Every phase is a **vertical slice**: it ships something you can
demonstrate end-to-end, not a horizontal layer.

**The loop for every phase** (per the delegation rules):
1. **Fable** writes `docs/plans/phase-N-plan.md` — goal, allowed files, forbidden files,
   approach, and the full test spec — *and improves the phase scope before handing off*.
2. **Implementer** (DeepSeek V4 Pro / Sonnet 5) builds inside the guardrails, runs tests,
   reports pass/fail.
3. **Opus 5** reviews results → approve, or precise fix instructions (one retry), or escalate.
4. Tests green → commit `feat(phase-N): ...` → push → deploy if flagged.
5. **Fable** signs off and writes the next phase plan.

**No phase is "done" until its gate passes.** The gate is a demonstrable behaviour, not
"the code exists."

---

## Track R — Brand & Campaign Registration (runs in PARALLEL from day 1)

**Highest-priority non-code task.** See `docs/BRAND_REGISTRATION.md`.

Registration is a **wall-clock wait, not a work item** — days to weeks of third-party
vetting that no engineering speed makes faster. It does not belong at P4 in sequence; the
*automation* belongs at P4, but **the first brand and campaign must be submitted manually
through the Bandwidth dashboard now, in parallel with P0.**

Consequences of being late:
- Unregistered numbers get Bandwidth error **`4476` blocked-unregistered** — rejected, not queued.
- **Throughput is Trust-Score-driven since March 2026**, not a flat 1 MPS. Late or sloppy
  registration permanently caps send rate.
- **Toll-free verification is a separate 3–6 week track.** 10DLC approval does not cover it.

---

## Toolchain constraints on the dev machine (measured 2026-08-26)

| | Found | Consequence |
|---|---|---|
| Python | **3.10.10** | **Pipecat needs 3.11/3.12.** Upgrade is a tracked risk with a P7 deadline. P0 targets what's installed. |
| Node | v22.19.0 | fine |
| **Docker** | **not installed** | P0 cannot rely on Compose locally. Test strategy must work without it; Compose still ships for the VPS. |
| **Postgres/psql** | **not installed** | Local test DB strategy is decided in `docs/plans/phase-0-plan.md`; a real-Postgres CI job is mandatory so Postgres-only SQL can't slip through. |

---

## P0 — Foundation
**WS:** 0, 10 · **Deploy:** yes (skeleton) · **Blocker risk:** R1

Fork `full-stack-fastapi-template`. Docker Compose (Postgres, Redis, MinIO). FastAPI boot
with a **settings object that validates `.env` and logs exactly which providers are disabled
and why**. Alembic. Orgs / users / roles / RBAC / multi-tenancy schema. JWT auth. Structured
logging + error taxonomy. CI. Deploy script to the VPS.

**Gate:** `/healthz` green locally *and* on the VPS. A second org cannot read the first
org's rows (test, not inspection). `pytest` green in CI.

**Also in P0 — resolve R1:** confirm we have a Bandwidth account path that reaches
production. This blocks everything downstream; do it first.

---

## P1 — Carrier layer + first SMS round-trip
**WS:** 1, 2 · **Deploy:** yes

The CAL interface (`event in → command out, async` — ARCHITECTURE D2). Bandwidth messaging
adapter. Webhook ingest with **Basic-auth verification, idempotency on `message.id`, and
state-based handlers**. Message + thread models. Send API. Inbound handler. DLR state machine
(`message-received | message-sending | message-delivered | message-failed`, with the 4-digit
error taxonomy mapped).

**Gate:** send an SMS from the API to a real handset; reply from the handset and see it land;
DLR recorded. **Replay the same webhook 3× out of order — state must be identical.**

---

## P2 — Contacts, conversations, inbox v1
**WS:** 2, 7 · **Deploy:** yes

Contacts, companies, custom fields, tags, notes. Conversation threading. Sticky sender.
React console: unified inbox, thread view, compose, send/receive live.

**Gate:** hold a full two-way SMS conversation entirely from the browser, with the thread
correctly attributed to a contact.

---

## P3 — MMS, templates, and the compliance core
**WS:** 2, 8 · **Deploy:** yes

Media pipeline to S3 (3.75 MB limit, 48 h carrier hosting → we re-host). MMS send/receive.
Templates + merge fields. **Compliance gate in the send path**: consent ledger (append-only),
STOP/HELP/START engine, quiet hours in the *recipient's* timezone, DNC list, opt-out
auto-reply.

**Gate:** **send STOP to number A → a send from number B in the same pool is blocked.**
A send scheduled into the recipient's local 21:00 is deferred, not dropped. MMS round-trips
with the media stored in S3 and rendered in the inbox.

---

## P4 — Numbers + 10DLC + toll-free verification
**WS:** 1, 8 · **Deploy:** yes · **Start early — vetting takes days-to-weeks (R5)**

Number search / order / release / configure (application + location assignment). Port-in with
LNP check. Brand + campaign registration API. TFV submission with webhook on approve/deny.
Number inventory UI.

**Gate:** order a number from the console and immediately send *and* receive on it. A brand
and campaign exist in TCR and the number is linked to the campaign.

---

## P5 — Voice core
**WS:** 3 · **Deploy:** yes

Outbound call creation. Inbound answer. BXML builder covering the verb set. Command→BXML
translation per D2. **Call *and leg* state machines** (a call is N legs — R7 and D6).
Recording to S3 + playback. Blind transfer. DTMF gather. Call log UI. AMD wired
(`async` mode, `machineDetectionComplete`).

**Gate:** place an outbound call and receive an inbound one; recording is playable from the
console; a blind transfer completes; DTMF input is captured; the leg state machine survives
a transfer without corrupting call state.

---

## P6 — Browser softphone for human agents
**WS:** 3, 7 · **Deploy:** yes

**Starts with a decision spike** — Bandwidth In-App Calling vs Telnyx WebRTC vs
SIP.js→own stack. ARCHITECTURE D1 says WebRTC for humans; the spike picks *which* WebRTC.
Note R-flag: Bandwidth's original WebRTC API is closed to new purchases since May 2023, and
In-App Calling has no independent latency benchmarks.

Then: browser softphone — inbound ring, outbound dial, **per-call caller-ID picker**, hold,
mute, DTMF, device selection, reconnection.

**Gate:** answer an inbound call and place an outbound one from the browser, choosing the
caller ID per call. Ear-test the audio; no one-way audio, no dead air.

---

## P7 — Media streaming + echo bot  ← **the risk phase**
**WS:** 4 · **Deploy:** yes · **Gate R2 first**

**Before writing the pipeline: measure the VPS ↔ Bandwidth media PoP leg** (RTT, loss,
jitter). If it is long-haul, region-pin the media host. Do not proceed and hope.

Then: WSS media server. `<StartStream mode="bidirectional">`. Frame codec (µ-law 8 k ↔ PCM ↔
16 k resample). Pacing + jitter buffer. `playAudio` write-back. **`clear` barge-in primitive.**
DTMF via the separate voice webhook (it does *not* arrive over the media WS). Latency harness.
**The conversation-replay test — this is the artifact that gates every later audio commit.**

**Gate:** a bidirectional echo bot. Measured round-trip published. `rt ≈ 1.0` **and** standing
queue depth measured separately (rt alone proves nothing — D5). No underruns over a 5-minute call.

---

## P8 — AI voice agent v1
**WS:** 4 · **Deploy:** yes

Pipecat + `Bandwidth/pipecat-bandwidth`. Deepgram Nova-3 → LLM → ElevenLabs Flash v2.5
(`ulaw_8000`). VAD/endpointing tuning — **this is the biggest latency lever, tune it first**.
Barge-in wired through all five steps (D5). Transcript capture. **STT bake-off: Nova-3 vs
AssemblyAI on real recorded caller audio (R3).**

**Gate:** **p50 ≤ 700 ms, p95 ≤ 1100 ms** voice-to-voice, measured and published.
Conversation-replay test green. Barge-in interrupts mid-sentence without deleting the tail.
The four classic barge-in bugs each have a regression test.

---

## P9 — AI voice agent v2
**WS:** 4, 5 · **Deploy:** yes

Function/tool calling (CRM lookup, book appointment, transfer to human). **Warm handoff to a
human seat** with context carried across the bridge — SIP REFER alone strips context, and the
carrier API doesn't solve this for us. Knowledge base / RAG grounding. AMD → voicemail drop
with beep detection.

**Gate:** the agent books an appointment via a tool call and warm-transfers to a human who
receives the context. Voicemail drop lands without clipping the message start.

---

## P10 — AI SMS agent
**WS:** 5 · **Deploy:** yes

Shared brain with WS-4 — one tool layer, two surfaces. Conversation state machine. Per-thread
memory. Handoff keywords + turn ceiling. Guardrails. Nothing to fork here: no
production-grade OSS AI SMS agent exists.

**Gate:** the agent holds a 10-turn SMS conversation, calls a tool, and hands off to a human
on the right trigger. Compliance gate still enforced on every AI-originated send.

---

## P11 — Outbound engine: dialer, scheduler, list upload
**WS:** 6, 7 · **Deploy:** yes

**List upload:** CSV/XLSX, column mapping UI, dedupe, phonenumbers validation + line-type,
DNC scrub on import, error report per rejected row.
**SMS scheduler:** campaign builder, per-number pacing with jitter, daily caps, new-number
warm-up ramp, sticky sender, retries.
**Auto-dialer:** preview → power → parallel (N lines, first answer wins) → predictive with
abandon-rate pacing. AMD, wrap-up timer, dispositions, retry backoff, local presence (off by
default — it lifts pickup but regulators have targeted the pattern).

**Gate:** a 500-contact list runs to completion respecting quiet hours, daily caps and DNC,
with a per-row outcome report. The dialer connects an agent to a live human and correctly
classifies a voicemail.

---

## P12 — IVR, queues, voicemail, routing
**WS:** 3, 7 · **Deploy:** yes

IVR builder (DTMF + speech). Ring groups, simultaneous/sequential ring. Queues + hold music +
callback queuing. Business hours + holiday routing. Voicemail + transcription. Whisper /
barge / monitor.

**Design constraint (R7):** Bandwidth conferences cap at 20 participants / 24 h, allow only 6
verbs inside, and **you cannot `StartStream` a conference room** — an AI joining a multi-party
call must stream on its own leg *before* joining.

**Gate:** a call routes through a 2-level IVR into a queue, hits hold music, reaches an agent,
and a supervisor whispers to that agent without the caller hearing.

---

## P13 — Analytics + platform services
**WS:** 9, 7 · **Deploy:** yes

Dashboards. Transcript search. Sentiment + call scoring. Usage metering per tenant
(OpenMeter). Public REST API + outbound webhooks (Svix). Scoped rotatable API keys. Audit logs.

**Gate:** a tenant's SMS/minutes/AI-token usage reconciles against carrier-reported usage
within tolerance. An external endpoint receives a signed outbound webhook and a replay is
deduped.

---

## P14 — Failover, hardening, production
**WS:** 1, 8, 10 · **Deploy:** yes

Telnyx adapters for messaging *and* voice (Ed25519 webhook verification — a different verify
path from Bandwidth's Basic auth). Automatic failover on the configured error threshold +
cooldown. Load test. Security review. Backups + restore drill. Number reputation monitoring.
Runbook. Status page.

**Gate:** kill Bandwidth credentials mid-run; traffic flips to Telnyx and back after cooldown,
with no message or call lost. **Restore from backup into a clean box and pass P1–P13 smoke tests.**

---

## Dependency graph

```
P0 ──┬── P1 ── P2 ── P3 ──┬── P4 ──────────────┐
     │                     └── P10 (AI SMS) ───┤
     └── (P0 gates all)                        │
                                               │
     P1 ── P5 ──┬── P6 (softphone) ── P12 ──────┤
                └── P7 ── P8 ── P9 ─────────────┤
                                               │
     P3 + P5 ── P11 (outbound engine) ──────────┼── P13 ── P14
                                               │
```

**Parallelizable once P1 lands:** the messaging lane (P2→P3) and the voice lane (P5→P6/P7)
are independent. P4 should start as early as possible because vetting is a wall-clock wait,
not a work item.
