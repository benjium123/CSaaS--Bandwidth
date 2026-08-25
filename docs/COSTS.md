# Cost Model

USD, August 2026. Sources in `docs/research/carrier-pricing.md` and inline below.
`⚠` = unverified / secondary source.

> **Two caveats that govern everything here.**
> 1. **Bandwidth does not publish a real rate card.** Its public page is a marketing floor.
>    Every Bandwidth-based number below is either their public figure (marked as such) or a
>    Telnyx proxy. **Get a signed rate sheet before committing to a cost model.**
> 2. **Carrier surcharges are not optional.** AT&T/T-Mobile/Verizon per-segment pass-through
>    fees add **~$0.0043/segment**, i.e. **+45% to +90%** on top of any advertised SMS rate.

---

## 1. The headline numbers

### One AI voice call, 5 minutes

| Line item | Cheap stack | Premium stack |
|---|---|---|
| | self-hosted Whisper + Kokoro + DeepSeek Flash (cached) | Deepgram Nova-3 + ElevenLabs Turbo + Claude Opus 5 (cached) |
| Carrier minutes (Telnyx ~$0.007/min × 5) | $0.0350 | $0.0350 |
| STT | $0.0025 | $0.0385 |
| TTS | $0.0017 | **$0.2250** |
| LLM (whole call, prompt-cached) | $0.0015 | $0.0598 |
| Recording storage (R2) | ~$0.0001 | ~$0.0001 |
| Infra amortization | $0.0050 | $0.0100 |
| **TOTAL** | **≈ $0.046** | **≈ $0.368** |

### One AI SMS conversation, 10 messages (5 in / 5 out)

| Line item | Cheap | Premium |
|---|---|---|
| Carrier (5 out @ $0.008 + 5 in @ $0.0065, incl. surcharges) | $0.0725 | $0.0725 |
| LLM (5 calls, cached) | $0.0003 | $0.0112 |
| **TOTAL** | **≈ $0.073** | **≈ $0.084** |

> **Carrier fees are 86–97% of SMS cost at every scale.** Which LLM you pick is almost
> irrelevant to SMS economics. The lever is carrier rate and **segment count**.

### One human-agent call minute ⚠ ESTIMATE — not primary-sourced
Fully-loaded US agent $18–35/hr → **$0.30–0.58/min**; offshore VA $6–10/hr → $0.10–0.17/min.
Plus carrier ~$0.007/min and seat amortization ~$0.01–0.02/min.
**Total ≈ $0.12–0.60/min, labour-dominated.** Needs a real citation pass before it goes
anywhere client-facing.

---

## 2. Monthly cost at scale

AI voice, blended per-minute: **cheap ≈ $0.0186**, **premium ≈ $0.0717** (cloud-API stacks).

| Call-min/mo | Cheap variable | + infra | **Cheap total** | Premium variable | + infra | **Premium total** |
|---|---|---|---|---|---|---|
| 1,000 | $18.60 | ~$100 | **~$119** | $71.70 | ~$100 | **~$172** |
| 10,000 | $186 | ~$170 | **~$356** | $717 | ~$170 | **~$887** |
| 100,000 | $1,860 | ~$600 | **~$2,463** | $7,170 | ~$600 | **~$7,773** |

SMS, blended $0.007/segment carrier:

| Segments/mo | Cheap total | Premium total |
|---|---|---|
| 10,000 | ~$72 | ~$81 |
| 100,000 | ~$715 | ~$812 |
| 1,000,000 | ~$7,150 | ~$8,120 |

---

## 3. Build vs buy — what we save

Bundled AI-voice platforms, per minute, **telephony NOT included** (add ~$0.007–0.015/min):

| Platform | $/min |
|---|---|
| Gemini 3.1 Flash Live | $0.023 |
| OpenAI Realtime mini | $0.030 |
| Deepgram Voice Agent (BYO model) | $0.050 |
| Vapi (platform fee only, excludes model cost) | $0.050 |
| ElevenLabs Conversational AI | $0.080 |
| OpenAI Realtime flagship | $0.096 |
| Retell (all-in worked example) | ~$0.11 |
| Bland | $0.11–0.14 |

A 5-minute call: cheapest bundled ≈ **$0.29 incl. telephony**; Retell/Bland ≈ **$0.55–0.70**.

> **Our cheap self-built stack (~$0.046) is 6–12× cheaper than buying. Even the premium
> self-built stack (~$0.368) beats the cheapest bundled competitor by ~1.5–2×.**
> This is the economic case for the whole project.

---

## 4. Component unit costs

### STT
| Provider | $/min | $/hr |
|---|---|---|
| **Soniox** | $0.0020 | **$0.12** |
| **AssemblyAI Universal-Streaming** | $0.0025 | **$0.15** — bills full session duration, not talk time |
| **Deepgram Nova-3** (PAYG list) | $0.0077 | $0.46 — promo $0.0048 through 2026-09-12 |
| Deepgram Nova-3 batch | $0.0043 | $0.26 |
| AWS Transcribe streaming | $0.0100 | $0.60 |
| Google Chirp 3 realtime ⚠ | ~$0.016 | ~$0.96 |
| Azure Speech ⚠ | ~$0.0167 | ~$1.00 |
| Speechmatics ⚠ | — | $0.40–1.35 (sources disagree 2.5×) |

**Self-hosted faster-whisper:** L4 @ $0.44/hr ÷ ~18 streams ≈ **$0.024/hr-of-audio**;
L40S @ $0.75–1.57/hr ÷ 25–100 streams ≈ **$0.030–0.063/hr**.
⚠ Concurrency figures are VRAM/throughput ceilings, **not** validated sub-300 ms latency
load tests. Treat as best case.

### TTS (900 chars/min ≈ 150 wpm)
| Provider | $/1M chars | $/min |
|---|---|---|
| Google Standard/WaveNet | $4 | **$0.0036** |
| OpenAI tts-1 | $15 | $0.0135 |
| Google Neural2 | $16 | $0.0144 |
| Azure Neural HD | $22 | $0.0198 |
| **Deepgram Aura-2** | $27–30 | $0.024–0.027 |
| Rime Mist v3 | $30 | $0.027 |
| Cartesia Sonic (Scale) | $37.4 | $0.0337 |
| **ElevenLabs Flash/Turbo (flat API)** | $50 | **$0.045** |
| ElevenLabs Multilingual (Pro sub) | $165 | $0.1485 |

Self-hosted **Kokoro-82M**: ~$0.021/hr-of-speech on A100 (RTF ≈ 0.03, ~50 concurrent).

> **TTS is the single biggest swing in the premium stack** — ElevenLabs alone is ~61% of a
> premium call's cost. Swapping to Aura-2 or Google Neural2 cuts premium-stack cost >60%.

### LLM
| Model | In $/1M | Out $/1M | Cache read $/1M |
|---|---|---|---|
| Claude Opus 5 | $5.00 | $25.00 | $0.50 |
| Claude Sonnet 5 | $2.00 | $10.00 | $0.20 |
| Claude Haiku 4.5 | $1.00 | $5.00 | $0.10 |
| GPT-5.6 Terra | $2.00 | $12.00 | $0.20 |
| GPT-5.6 Luna | $0.20 | $1.20 | $0.02 |
| **DeepSeek V4 Pro** (off-peak, cache hit) | $0.022 | $1.98 | — |
| **DeepSeek V4 Flash** (off-peak, cache hit) | $0.007 | $0.66 | — |
| Groq Llama 3.1 8B Instant | $0.05 | $0.08 | — |
| Groq GPT-OSS 120B | $0.15 | $0.60 | $0.075 |
| Gemini 3.5 Flash-Lite | $0.30 | $2.50 | $0.03 |

**5-min call token math** (18 turns each way, 1,000-token system prompt, history resent
every turn → quadratic growth): **34,020 input / 1,080 output tokens.**

| Model | No cache | Cached |
|---|---|---|
| DeepSeek V4 Flash | $0.0082 | **$0.0015** |
| Claude Sonnet 5 | $0.0788 | $0.0236 |
| Claude Opus 5 | $0.1971 | **$0.0598** |

> **Prompt caching saves 70–81%** because resent history dominates. Build it in from P8,
> not as an optimization later.

---

## 5. Infrastructure

- **Compute:** DO 8vCPU/16GB $168/mo; 16vCPU/32GB $336/mo; AWS m6i.2xlarge $280/mo.
  ⚠ Sizing estimate (no public Pipecat benchmark): **~0.3 vCPU / 200 MB per concurrent
  voice session** → ~$6.15–6.46/concurrent-session/month on DO.
- **Postgres/Redis:** DO managed 1vCPU/1GB ≈ $15/mo each.
- **Recordings, 10,000 min/mo dual-channel:** raw 8 kHz 16-bit = 1.92 MB/min → **19.2 GB/mo**;
  compressed 64 kbps → **4.8 GB/mo**. Storage: S3 $0.44/$0.11, **R2 $0.29/$0.07**, B2
  $0.13/$0.03, MinIO ~$0.
  **Egress is $0 on all four at this volume.** R2's zero-egress advantage only matters past
  ~1M min/mo, where S3 starts charging ~$24/mo.
- **Media bandwidth:** G.711 duplex = 57.6 MB/hr. 1,000 call-hours = 57.6 GB/mo — trivial
  against any VPS's included transfer.

**Self-hosted GPU break-even** (derived, not sourced): a dedicated rig (L40S + A100 ≈
$1,879/mo) only beats cloud APIs above **~180,000 call-min/mo**. A single shared L4
($321/mo) breaks even at **~30,000 min/mo** — that is the realistic first move, not a rig.

---

## 6. Where the money actually goes

| Scale | Dominant driver | Highest-leverage move |
|---|---|---|
| Single call, cheap stack | **Carrier minutes (~76%)** | Negotiate a volume tier. Carrier is the floor now, not AI. |
| Single call, premium | **TTS (~61%)** | Swap ElevenLabs → Aura-2/Google Neural2. >60% cut. |
| 1K–10K min/mo | **Fixed infra (48–84%)** | Don't over-provision. One small VPS. **No dedicated GPU** — it loses money here. |
| 100K min/mo | Variable cost (75–92%) | Prompt caching (−70–80% LLM), cheaper TTS tier, cross the GPU break-even. |
| **SMS, every scale** | **Carrier/A2P (86–98%)** | Carrier rate + **minimize segment count**. LLM choice is noise. |
| AI vs human | **Human labour is 5–40× AI per minute** | **Shifting volume from human agents to AI is the biggest structural lever in the system** — far bigger than which AI vendor you pick. |

---

## 7. Suggested product pricing tiers

Cost basis: cheap stack ≈ $0.019/AI-voice-min and ≈ $0.0073/SMS segment landed.
Target ~70% gross margin, which these tiers clear comfortably.

| Tier | Monthly | Included | Overage | Notes |
|---|---|---|---|---|
| **Starter** | $99 | 1 number, 2 seats, 1,000 AI min, 2,500 SMS segs | $0.09/min, $0.03/seg | Covers ~$120 of blended cost at the cap; margin comes from under-use. |
| **Growth** | $399 | 5 numbers, 10 seats, 6,000 AI min, 25,000 segs | $0.07/min, $0.025/seg | The volume band where infra is already paid for. |
| **Scale** | $1,499 | 25 numbers, 40 seats, 30,000 AI min, 150,000 segs | $0.05/min, $0.02/seg | Crosses the shared-L4 GPU break-even — self-host STT here. |
| **Enterprise** | custom | unlimited seats, dedicated numbers, SLA, SSO | negotiated | Requires a negotiated carrier rate sheet to price safely. |

**Pass-through, billed at cost on every tier** (never absorb these):
carrier A2P surcharges · 10DLC brand + campaign fees · toll-free verification · number
rental beyond the included count · port-in fees.

⚠ These tiers are a **starting proposal**, not a validated pricing study. They assume the
cheap stack. Re-derive once a Bandwidth rate card exists.

---

## 8. Known gaps
Speechmatics, Azure, Google Cloud, Hetzner, AWS RDS/ElastiCache and DO bandwidth-overage
pricing pages are JS-rendered and returned no numeric tables — those figures are secondary
and disagree by up to 2.5×. The full Telnyx SIP-trunk rate sheet is gated. T4/A10 rental
rates have largely vanished from RunPod/Vast.ai 2026 listings. Human-agent labour cost and
softphone seat pricing are unverified estimates.
