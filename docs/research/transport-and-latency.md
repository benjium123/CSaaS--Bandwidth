# Research — SIP vs WebRTC vs WebSocket media, and the real latency budget

This is the file that answers "SIP or WebRTC?". Read it before arguing about transport.

## Terminology, stated precisely (people conflate these constantly)

| Thing | What it actually is |
|---|---|
| **SIP** | Signaling only (INVITE/BYE/REFER). Sets up, tears down, transfers. **Carries no audio.** |
| **RTP/SRTP** | The media transport. Runs over **UDP**. This is the actual audio. |
| **WebRTC** | A browser *media stack* — ICE (NAT traversal), DTLS-SRTP (mandatory encryption), built-in jitter buffer, AEC, PLC. Still RTP/SRTP over UDP underneath. Needs *some* signaling, which can be SIP-over-WebSocket (SIP.js/JsSIP) **or** a vendor's proprietary JSON protocol. |
| **WS media streaming** | A third thing. Base64 raw codec payload (PCMU 8 kHz) framed in JSON over **WSS/TCP**, server-to-server. No SIP, no ICE, no DTLS. Bandwidth `<StartStream>`, Telnyx `streaming_start`, Twilio `<Stream>`. |

**"SIP vs WebRTC" is a false binary.** WebRTC is not a rival to SIP; it is a rival to a
legacy SIP *softphone*. The real question is: **where does the audio terminate, and does it
ride UDP or TCP.**

## The four real options

### Option A — Carrier-managed WebRTC
Bandwidth In-App Calling / Telnyx `@telnyx/webrtc`.
- ⚠ **Bandwidth's original WebRTC API has been closed to new purchases since May 2023** —
  new customers get "In-App Calling" instead. Telnyx's WebRTC JS SDK is live and open.
- Latency: minimal for the browser leg — browser owns jitter buffer, AEC, PLC. ~1 hop.
  Telnyx quotes sub-200 ms RTT on their backbone with co-located AI.
- Ops: **lowest.** No SIP stack, no RTPengine, no TURN to run.
- Cost: Telnyx WebRTC leg $0.002/min + PSTN leg. Bandwidth folded into In-App Calling,
  pricing on request.
- **Makes impossible:** raw frame access before STT, media-layer multi-carrier failover,
  custom jitter tuning. You are hostage to one vendor's gateway — and Bandwidth has already
  killed and relaunched this product line once.

### Option B — Full SIP stack you own
Kamailio/OpenSIPS + RTPengine + coturn, or FreeSWITCH, or jambonz (drachtio + FreeSWITCH +
rtpengine + Homer), or LiveKit SIP. Browser = SIP.js/JsSIP over WSS.
Both carriers terminate standard SIP trunks (BYOC).
- Latency: **lowest media path of all four** — RTP stays UDP end-to-end. Only extra hop is
  your own SBC/relay: sub-5–10 ms on a well-provisioned VPS, real if under-provisioned.
- Ops: **highest.** SIP registration edge cases, NAT/ICE, RTPengine tuning, WSS cert
  rotation, DTMF relay, codec negotiation, **SBC security (toll fraud is the #1 attack
  surface)**. Ongoing commitment, not a one-time setup.
- Failure modes: re-INVITE storms, one-way audio from NAT/ICE mismatch, fragile
  OPTIONS-ping registration health. Exactly the bug class already fought in the
  MightyCall/dispo system.
- **Makes possible (and nothing else does):** native SIP REFER warm transfer, native
  multi-party conferencing, supervisor whisper/barge as a first-class primitive,
  multi-carrier failover under one stack, complete independence from carrier roadmaps.

### Option C — Provider WebSocket media streaming, no SIP stack
Your VPS runs a WSS server; carrier streams base64 PCMU 8 kHz / 20 ms frames as JSON.
- Latency: small extra hop **if same region** — but rides **TCP**.
- Ops: low-medium. No SIP/ICE/DTLS, but you own reconnect/heartbeat and codec transcoding
  (µ-law ↔ PCM ↔ 16 kHz resample for STT).
- Documented failure mode: **idle WS connections silently dying mid-call after ~60–70 s of
  silence with no close handshake** (live Pipecat issue #3699) — TCP hasn't detected the
  failure because nothing was sent.
- **Makes impossible:** this can never be a *browser* softphone transport. A browser cannot
  do raw base64 PCMU framing with acceptable jitter handling and has no native jitter
  buffer/AEC for it. Pipecat's own docs: *"you shouldn't use WebSockets for edge-to-cloud
  realtime audio."*

### Option D — Hybrid
Humans on a real WebRTC path; AI agents on carrier WS media streaming.
This is what most production voice-AI platforms actually run. A server process doesn't need
a browser's AEC and jitter buffer, and the TCP downside is manageable **if the WSS hop is
short and same-region**.

## The latency budget, with real numbers

| Component | Low | Typical | High |
|---|---|---|---|
| VAD compute itself | <1 ms | 1–5 ms | 10 ms |
| **Endpointing / silence-wait** | 100 ms | **300–800 ms** | 900 ms |
| Jitter buffer (fixed) | 30 ms | 40–80 ms | 100 ms |
| Jitter buffer (adaptive max) | — | — | 100–200 ms |
| STT streaming | ~100 ms (co-located) | 300 ms | 992 ms TTFT |
| LLM time-to-first-token | 120–200 ms (Groq LPU) | 225–400 ms | 650–700 ms (GPT-4o cross-region) |
| TTS time-to-first-byte | 40 ms (Cartesia Sonic Turbo) | 80–188 ms | 1.73 s (one ElevenLabs benchmark) |
| **Network ingress + signaling (SIP or WS)** | **45 ms co-located** | **50–150 ms** | **150–200 ms cross-region** |

**Measured end-to-end (caller stops → agent audio starts):**

| System | p50 | p95 |
|---|---|---|
| Telnyx co-located best case | 450 ms | — |
| Retell (production) | 680 ms | 920 ms |
| Vapi (production) | 700–720 ms | 1,050 ms |
| Bland | 850 ms | 1,180 ms |
| Typical stitched multi-vendor stack | ~1,210 ms | — |
| ElevenLabs fixed stack (one benchmark) | 1,730 ms | — |

**Sub-800 ms is real** — multiple vendors hit it in production.
**Sub-500 ms requires tight co-location + premium fast models** (same-datacenter STT/LLM/TTS,
Groq-class LLM, aggressive endpointing). It is not the median for a stitched stack.
**>1.2–1.5 s is where callers consciously notice walkie-talkie turn-taking and talk over the agent.**

## THE VERDICT ON TRANSPORT

**Transport is 45–200 ms out of a 450–1,800 ms total — roughly 3–15% of the budget.**
Endpointing + STT + LLM + TTS are the other **85–97%**.

The single largest controllable lever is the **endpointing/VAD silence wait (300–800 ms)** —
bigger than the entire transport layer, bigger than TTS, comparable to or bigger than LLM TTFT.

> **Running our own SIP stack purely to shave AI latency is not justified.**
> The win from owning SIP, if we ever take it, is **operational** — warm transfer,
> conferencing, supervisor whisper, multi-carrier failover — **not latency.**

**The one caveat that does bite:** this holds only while the WS/TCP leg is short and
same-region. A long-haul WS leg blows past all of these numbers via TCP retransmit stalls —
that *is* a transport-choice effect, just not a "SIP signaling overhead" one.

## RTP/UDP vs WebSocket/TCP — the real mechanism

TCP guarantees ordered complete delivery, so on packet loss it **halts everything behind the
lost packet** until retransmission succeeds (head-of-line blocking). For live audio, a 200 ms
wait to recover a lost syllable fragment creates silence far more disruptive than a glitch.
RTP/UDP just drops the frame and lets PLC/FEC paper over it — a click, not a stall.

Pipecat states this as settled: *"TCP retransmits lost packets and holds up everything
behind them. For audio, a dropped packet is better discarded than waited for."*
Daily.co concedes the same problem with Twilio WS Media Streams, and states their mitigation
is **placing infrastructure physically close to the carrier's voice servers.**

**This corroborates our own production finding of 300–650 ms/sec of dead air on a long-haul
TCP audio leg** — entirely consistent with TCP RTO backoff under WAN jitter/loss.

**Mitigations, by leverage:**
1. **Region-pin / co-locate** the media-consuming process with the carrier's media PoP.
   Highest-leverage fix, and what every vendor recommends first.
2. **Keep any TCP/WS leg short.** Never route a human operator's live audio, or chain
   operator-PC ↔ VPS ↔ carrier, over TCP. Matches our existing CALLING LAW #1.
3. **Prefer RTP/UDP wherever media touches a real endpoint** (browser, human ear).
   Reserve WS/TCP for the narrow carrier-integration seam only.
4. **Tune jitter buffer / ptime** — 20 ms ptime, adaptive buffer 40–100 ms (cap 150–200 ms).
   Note this safety net **only exists on UDP/RTP**; a WS/TCP leg doesn't get it at all.
5. **Heartbeats + application-level dead-connection detection** on any WS leg — TCP will
   not tell you the connection died.

## Browser softphone comparison

| | Provider WebRTC SDK | SIP.js/JsSIP → own stack | Headless-browser webphone |
|---|---|---|---|
| Audio quality / AEC | Native browser WebRTC — best-in-class, zero effort | **Same** (SIP.js only does signaling; media is still native WebRTC) | Degraded |
| Hold / mute / DTMF | Out of the box | Full SIP semantics | Fragile |
| Warm transfer | Vendor abstraction only | **Native SIP REFER** | No |
| Conference / whisper / barge | Awkward, API-constrained | **First-class primitive** | No |
| iOS Safari | Works; native app still recommended | Same caveat | No |
| Per-call caller ID | **Easiest** — pass `from` per call | Harder — set From/P-Asserted-Identity per INVITE (this is what our bridge.js picker already does) | n/a |
| Reconnection | Vendor SDK owns it | **You own it** | n/a |

**Headless-browser webphone is disqualified as a primary human path by our own measurements:**
headless Chrome on Windows renders audio at **0.68x under call load** (only headed Chrome
gets the real WASAPI clock), and headless-under-Xvfb on a Linux VPS has no hardware clock at
all. Keep it exactly where it already is — VPS fallback seats and AI-only.

## Bridging AI + human in one call

| Architecture | Difficulty |
|---|---|
| **LiveKit-style room model** (or FreeSWITCH `mod_conference`) — caller = SIP participant, human = WebRTC participant, AI = server participant, all one room. Supervisor listen-in / AI whisper / warm transfer = "add a muted one-way participant." | **Easiest.** LiveKit documents Human-in-the-Loop and Supervisor patterns, including a private side-room for the AI to brief the human before bridging the caller in. |
| Own FreeSWITCH/Kamailio conference bridge | Medium — same capability, more plumbing you own |
| Carrier-managed WebRTC API alone | **Painful** — constrained to the vendor's bridge/transfer primitives. SIP REFER alone strips context; real integrations must carry call data across the bridge via external orchestration regardless. |

A well-engineered warm transfer with briefing takes **20–40 s** end-to-end before the caller
is bridged. Past 60 s abandonment rises measurably. This is an orchestration constraint,
independent of transport.

## ⚠ Unverified, flagged for follow-up
- Bandwidth In-App Calling maturity / latency — no independent 2026 benchmarks, vendor copy only.
- Real production p95 for a **self-hosted** Kamailio+RTPengine+FreeSWITCH AI stack — every
  published number comes from a managed platform (LiveKit/Telnyx/Daily). A hand-rolled stack
  could differ materially.
