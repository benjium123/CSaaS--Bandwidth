# Research — AI voice agent framework + STT/TTS/LLM selection

## Framework verdicts

| Framework | Stars | License | Status | Verdict |
|---|---|---|---|---|
| **Pipecat** (pipecat-ai/pipecat) | ~14k | BSD-2 | v1.0 Apr 2026, near-daily commits, Daily.co + community | **PICK THIS.** Built-in serializers: Twilio, Telnyx, Plivo, Exotel, Vonage, Genesys. Bandwidth is not in core — **but Bandwidth ships and maintains its own first-party add-on** (below). |
| **LiveKit Agents** | ~11–13k | Apache-2.0 | 1.5.x, active | Solid. Best telephony ergonomics (SIP is first-class, not a bolted-on serializer). But you adopt a whole WebRTC SFU stack — LiveKit server + livekit-sip + TURN. Common self-host failures: TURN misconfig behind corporate NAT, SFU capacity miscalc, weak SFU observability. Guidance: start on LiveKit Cloud, self-host past ~5k MAU. **Keep as the fallback if we ever need room-model AI+human bridging.** |
| **Vocode** (vocode-core) | — | MIT | Last PyPI 0.1.113 **Jun 2024**; last push **Nov 2024** | **DEAD.** Do not start here. No Telnyx integration either. |
| **jambonz** | 96–99★ core | MIT on repos — **BUT v10.x+ core requires a paid license key tied to your DNS domain** | Alive; commits within days | Genuinely battle-tested SIP (drachtio + FreeSWITCH + rtpengine + Homer). `llm()`/`s2s()` verb connects calls to OpenAI Realtime / Ultravox / ElevenLabs Conv-AI / Deepgram Voice Agent, with `toolHook` for function calling. **Highest ops burden of the group** — drachtio, rtpengine, FreeSWITCH, sbc-inbound, sbc-outbound, api-server, Redis, MySQL, InfluxDB. **Right tool only if we front a raw SIP trunk.** 🟡 Verify the license for the version you'd deploy. |
| **TEN Framework** | ~10.9k | OSS | Active | Multimodal graph orchestration, ships `ten-vad`. No Bandwidth adapter (⚠unverified). |
| **Bolna** | ~695 | OSS | Active mid-2026 | Telephony-first, declarative JSON agent config. No Bandwidth (⚠unverified). |
| **Dograh** (dograh-hq/dograh) | ~5.5k | **BSD-2** | Daily commits, 793 commits | Self-hosted Vapi/Retell alternative — Python, **visual workflow builder**, full dashboard, telephony via Twilio/Vonage/Telnyx/Plivo/Asterisk ARI, inbound+outbound, human handoff, Docker deploy. **Voice-only, no SMS/CRM.** Best harvest target for the AI-agent workflow builder. |
| **Moshi** (kyutai-labs) | — | Fully open | Research-grade | True full-duplex S2S via Mimi codec, ~200 ms practical on an L4. **No telephony transport, no production hardening.** R&D only. |
| **Layercode** | — | ⚠ likely closed SaaS | — | Do not assume self-hostable. |

## THE HEADSTART: `Bandwidth/pipecat-bandwidth`

BSD-2-Clause. **First-party, maintained by Bandwidth.** Tested against Pipecat v1.4.0,
Python 3.11/3.12. Does exactly what we need:
- Decodes inbound µ-law 8 kHz; encodes outbound µ-law/PCM at 8/16/24 kHz
- Terminates calls via the Bandwidth Voice API (OAuth2)
- **Implements barge-in via Bandwidth's `clear` WebSocket event**

Two caveats it documents, both load-bearing:
1. **DTMF does NOT arrive over the media WebSocket.** Handle it via a separate voice webhook.
2. **`call_id`/`account_id` must come from the authenticated inbound webhook — never trust
   them from the WS `start` event.** (This is an auth boundary, not a convenience.)

Adding a transport to Pipecat generally is mechanically easy — subclass `FrameSerializer`,
plug into `FastAPIWebsocketTransport`. That is exactly the pattern Bandwidth used. So even
if we outgrow their serializer, the shape is known.

`pipecat-flows` merged into core as of pipecat-ai 1.5.0 (`pipecat.flows` namespace); the
standalone `pipecat-ai-flows` package is deprecated. Gives graph-of-nodes conversation
structure with APPEND / RESET / RESET_WITH_SUMMARY context strategies on node transition.

## STT ranking for 8 kHz µ-law telephony

1. **Deepgram Nova-3** — fastest + cheapest (~$0.0077/min streaming ≈ $0.46/hr; batch
   $0.0043/min), partials every 100–250 ms, ~450 ms median streaming latency.
   ⚠ Accuracy claims are inconsistent (5.26% marketed WER vs 12–25% in independent
   telephone-condition benchmarks). **Validate on our own recorded caller audio.**
2. **AssemblyAI Universal-Streaming** — better independent accuracy (7.69% vs Deepgram's
   12.22% on one benchmark), P50 partial ~150 ms / P90 ~240 ms, but slower time-to-final
   (~760 ms) and pricier ($0.45/hr streaming).
3. **Speechmatics** — $1.04–1.35/hr, strongest accent/dialect handling.
4. **Soniox** — ~10% WER on multilingual code-switching; ⚠ no 8 kHz-specific numbers found.
5. **faster-whisper self-hosted** — 4–6× faster than vanilla via CTranslate2, 3–6 GB VRAM at
   int8/fp16, ~22–40 ms per chunk on a 4090. **But Whisper is trained mostly on ≥16 kHz and
   has no native streaming API** — bolt-on chunking lands ~1.5–2 s for a full utterance.
   Only if data residency or zero-per-minute-cost is a hard requirement.
6. Azure / Google — mature, not price- or latency-leading. Only for existing cloud lock-in.

**Pick: Deepgram Nova-3 primary, AssemblyAI as the accuracy fallback.**
Ship a WER bake-off against real recorded calls before locking it in.

## TTS ranking for telephony streaming

1. **Cartesia Sonic** — fastest (Sonic Turbo ~40 ms TTFB, Sonic-3 ~75–90 ms), cheapest at
   volume (~$0.039/1K chars). ⚠ Confirm the exact 8 kHz µ-law output-format parameter.
2. **ElevenLabs Flash v2.5** — ~75–90 ms TTFB, $0.05/1K chars, best naturalness at that
   latency tier, **has a documented `ulaw_8000` output mode.**
3. **Deepgram Aura-2** — ~200–313 ms P50 TTFB. Slower, but simplest if bundling with
   Deepgram for one-vendor billing.
4. **Rime** — use **Coda only** (sub-100 ms). Other Rime voices (Mist-v3, Arcana) benchmark
   as high-variance and unsuitable for real-time.
5. PlayHT / OpenAI TTS / Azure — slower, higher variance. OpenAI TTS-1-HD explicitly called
   out as unsuitable for real-time.
6. Kokoro / self-hosted — near-zero network latency if co-located on GPU, quality trails
   commercial premium. Cost-cutting option once quality is validated. ⚠ Benchmark TTFB.

**Pick: ElevenLabs Flash v2.5 first (documented ulaw_8000 = less transcode risk),
Cartesia Sonic as the speed upgrade after A/B.**

## Cascaded vs speech-to-speech

**Cascaded (STT → LLM → TTS) wins for us.** Reasons:
- **Explicit text transcript at every hop** — needed for compliance/audit and for feeding
  downstream classification/analytics.
- The LLM step can be prompted, tooled and function-called like any text agent.
- Mix and match cheapest/fastest per component.
- A well-engineered cascade (Deepgram + Groq + Cartesia) now **matches or beats S2S** on
  voice-to-voice latency.

S2S (OpenAI Realtime, Gemini Live) is a black box: can't swap the LLM, can't bolt on RAG or
business rules mid-turn, no clean transcript boundary, and costs more (Deepgram claims
OpenAI Realtime runs ~75% above their bundled agent API — self-interested but directionally
plausible given audio-token pricing).

**Use S2S only for simple receptionist/FAQ bots.** For anything driving business logic,
cascaded.

## Barge-in: how it works and how it breaks

The convergent pattern (Pipecat / LiveKit / jambonz all do this):
1. VAD detects caller speech above threshold while TTS is playing
2. Stop local audio playback immediately
3. **Cancel the in-flight TTS request**
4. Cancel or truncate the current LLM generation
5. **Send an explicit flush/clear to the carrier** so audio already buffered downstream
   stops playing — this is exactly Bandwidth's `clear` event

**The four classic bugs:**
- **Self-echo re-triggering barge-in** — the agent's own TTS leaks back (weak AEC on some
  SIP trunks) and trips its own VAD. Symptom: the bot repeatedly interrupts itself with no
  caller input.
- **False-positive VAD on background noise** — VAD tuned on clean studio audio misfires on
  car noise, open office, weak cellular, 8 kHz codec artifacts. **Leading real-world cause
  of unwanted interruptions.**
- **Cutting off sentence tails** — threshold too low fires on filler ("mm-hmm") and normal
  prosodic pauses.
- **Buffer-depth race** — even after a successful `clear`, audio already in the carrier's
  jitter buffer keeps playing for a beat. Account for buffer depth in timing expectations.

> This maps 1:1 onto our existing audio-pipeline law: **a queued frame may be shed only if
> that frame is itself silent (per-frame peak) — never because silence is arriving.**
> Carry that law into this codebase and gate every audio change on a conversation-replay test.

## Latency targets for this build
- **p50 ≤ 700 ms, p95 ≤ 1100 ms** voice-to-voice. Achievable with a standard cloud stack.
- Sub-500 ms only if we co-locate STT/LLM/TTS and use Groq-class inference. Treat as stretch.
- **Tune endpointing first.** It is 300–800 ms of the budget — more than everything else we
  can control.
