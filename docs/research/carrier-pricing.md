# Research — Carrier pricing, US, 2026 (USD)

Gathered 2026-08-26 from primary pricing pages where possible.

> **The single most important finding:** Bandwidth's public pricing page is a **marketing
> floor, not a rate card.** Telnyx, Twilio and Plivo publish real self-serve numbers;
> Bandwidth, Vonage, Sinch, Bird, Infobip and SignalWire gate the numbers that would actually
> appear on an invoice behind a login or a sales call.
> **Do not architect against Bandwidth's public $0.006 SMS / $0.0100 voice figures as if they
> were final. Get a signed rate card from an account rep.**

`[U]` = unverified / third-party aggregator · `[NP]` = not public

---

## Messaging — base rates

| Item | Bandwidth | Twilio | Telnyx | Plivo | Sinch | Vonage | Bird |
|---|---|---|---|---|---|---|---|
| SMS out (10DLC long code) | $0.0060 | $0.0083/seg | **$0.0040/part** | $0.0077 | $0.0078 | $0.00809 `[U]` | $0.00331 `[U]` |
| SMS in | not itemized | $0.0083/seg | $0.0040/part | $0.0077 | $0.0078 | $0.00649 `[U]` | $0.0030 `[U]` |
| MMS out | $0.0150 | $0.0220 | $0.0150/part | $0.0180 | ~3–5× SMS `[U]` | `[NP]` | `[U]` |
| MMS in | not itemized | $0.0165 | $0.0050/part | $0.0180 | `[NP]` | `[NP]` | `[NP]` |
| Toll-free SMS out/in | $0.0075 | $0.0083 both | $0.0055 both | $0.0079 both | $0.0078 both | `[NP]` | `[NP]` |
| Toll-free MMS | $0.0200 | $0.022 / $0.020 | $0.0160 both | $0.0200 both | $0.018 / $0.010 | `[NP]` | `[NP]` |

## Carrier surcharges — pass-through, **on top of** the above

**This is the most-missed line item in CPaaS cost modelling.** All providers pass through the
same TCR-set carrier fees at roughly cost — the differentiator is the vendor's base rate, not
the surcharge.

| Carrier | SMS out | SMS in | MMS out | MMS in |
|---|---|---|---|---|
| AT&T | $0.0035 | $0.0035 | $0.0090 | $0.0090 |
| T-Mobile | $0.0045 | $0.0025 | $0.0100 | $0.0100 |
| Verizon | $0.0045–$0.0050 | $0.0000–$0.0070 | $0.0070 | — |

> **Blended surcharge ≈ $0.0043/segment.** On a typical mixed-carrier 10DLC send that is
> **+45% to +90% on top of the advertised base rate.** Always model landed cost, never base.

## Voice

| Item | Bandwidth | Twilio | Telnyx | Plivo | SignalWire |
|---|---|---|---|---|---|
| Outbound US, /min | $0.0100 | $0.0140 | **~$0.0070** ($0.005 SIP + $0.002 API) | $0.0115 | $0.0080 |
| Inbound local, /min | $0.0055 | $0.0085 | ~$0.0052 | $0.0055 | $0.0066 |
| Inbound toll-free, /min | `[NP]` | $0.0220 | ~$0.0170 | $0.0180 | $0.0147 |
| WebRTC / browser leg, /min | `[NP]` | bundled | $0.0020 | `[NP]` | $0.0030 |
| Conference, /participant-min | `[NP]` | $0.0018 | $0.0020 | $0 listed (verify) | `[NP]` |
| Call recording, /min | `[NP]` | $0.0025 | $0.0020 | $0 (free) | `[NP]` |
| Recording storage | `[NP]` | **$0.0005/min/month recurring** | $0 | `[NP]` | `[NP]` |
| **Media streaming, /min** | `[NP]` | **$0.0040** | **$0.0035** | `[NP]` | `[NP]` |
| AMD, per call | `[NP]` | bundled | $0.0020 std / $0.0065 premium | `[NP]` | `[NP]` |
| Provider STT, /min | $0.0450 | ~$0.20 ($0.05/15s) | $0.0015 (Parakeet) – $0.027 (Azure) | `[NP]` | bundled in $0.16/min AI runtime |
| Provider TTS | `[NP]` | via Polly | $0.000003–$0.000048/char | `[NP]` | bundled |

> **Telnyx voice math trap:** the advertised "$0.002/min" is a *platform fee on top of* a
> separate SIP trunk rate. Real outbound ≈ **$0.007/min**, not $0.002.

> **For our AI voice build:** media streaming + AMD + recording + transcription on top of the
> base minute can **roughly double** the per-minute voice cost. Budget the stack, not the leg.

## Numbers

| Item | Bandwidth | Twilio | Telnyx | Plivo |
|---|---|---|---|---|
| Local number/month | `[NP]` | $1.15 | $1.00 → **$0.25 at 5,000+** | **$0.50** |
| Toll-free/month | "$1–a few dollars" (site copy) | $2.15 | $1.00 | $1.00 |
| Port-in fee | `[NP]` | `[NP]` | `[NP]` | `[NP]` |

**Port-in fees are unpublished by every vendor** — a support-ticket item everywhere.
Aggregators cite $10–$40/number but that is **not** primary-sourced. Do not assume $0.

## 10DLC registration

| Item | Twilio | Telnyx | Bandwidth |
|---|---|---|---|
| Brand, one-time | $44 std / $4 low-volume | TCR pass-through (~$4) | **$4** (passes through at cost) |
| Campaign vetting | $15 | pass-through `[U]` | not itemized |
| Campaign monthly, standard | $10/mo (range $1.50–$10) | ~$1.50–$30/mo `[U]` | **$10/mo std, $1.50/mo low-volume** |
| **Minimum commitment** | none stated | n/a | **3-month minimum — cancel early, still billed in full** (all use cases except Political) |
| Toll-free verification fee | `[NP]` — pages 403'd | `[NP]` | `[NP]` |

Registration fees are largely TCR-set and passed through near cost, so **the real spread
between providers on registration is small.** The meaningful differences are markup (little)
and **Bandwidth's 3-month lock-in**, which is a genuine startup-hostile term.

## Volume tiers

- **Telnyx** — the only provider with a real public self-serve volume ladder. SMS discounts
  start above 100M msg/mo, reaching **$0.0005/part above 1B/mo** (>85% off). Numbers:
  51–250 → $0.79/mo, 5,000+ → $0.25/mo. SIP channels: first 10 $12/mo → $8/mo above 250.
- **Twilio** — no public table; volume discounts via enterprise sales.
- **Bandwidth** — sales-negotiated. Public page is a floor. Likely competitive *only* via a
  negotiated deal — which is plausible, since Bandwidth is a Tier-1 carrier that many other
  CPaaS vendors resell on top of.
- **Plivo / Vonage / Sinch / Bird / Infobip / SignalWire** — no public tiers found.
- Free-tier/trial credits: **not confirmed for any provider.** Do not assume.

## Year-1 comparison
1,000 SMS segments (landed, incl. ~$0.0043 blended surcharge) + 1,000 outbound voice minutes
+ 1 local number × 12 months + 10DLC standard brand & campaign for a year.

| Provider | 1k SMS | 1k voice min | Number ×12 | 10DLC Y1 | **Total** |
|---|---|---|---|---|---|
| **Telnyx** | $8.30 | $7.00 | $12.00 | ~$22–$364 | **~$49–$391** |
| **Plivo** | $11.70 | $11.50 | $6.00 | ~$124 `[U]` | **~$153** |
| **Bandwidth** | $10.50 | $10.00 | ~$12–$24 | $124 | **~$160–$168** |
| **Twilio** | $12.60 | $14.00 | $13.80 | $164 | **$204** |
| Vonage / Sinch / Bird / Infobip / SignalWire | — | — | — | — | **not reliably computable** |

Only **Telnyx, Bandwidth, Twilio and Plivo** have enough primary-sourced numbers to trust.

## Rankings

**High-volume SMS:** Telnyx (cheapest verified base + the only public volume curve) → Bird
`[U]` → Plivo → Bandwidth → Sinch → Twilio.

**High-volume outbound voice:** Telnyx (~$0.007 verified) → SignalWire ($0.0080) →
Bandwidth ($0.0100 list, **likely much better negotiated** — it's a Tier-1 carrier) →
Plivo ($0.0115) → Twilio ($0.0140).

**Low-volume / startup:** Plivo (lowest number rental, no minimum) → Telnyx (transparent,
no contract) → Twilio (best docs, worst unit economics) → **Bandwidth (worst self-serve
transparency + a 3-month 10DLC lock-in)**.

## The 11 hidden costs

1. **Carrier surcharges add 30–90% to the advertised SMS rate.**
2. **Telnyx's "$0.002/min" is a platform fee, not the call cost.** Real ≈ $0.007/min.
3. **Media streaming is billed per minute on top of the call** (Twilio $0.004, Telnyx $0.0035).
   For an AI voice agent, streaming + AMD + recording + transcription roughly **doubles** the minute.
4. **Recording storage recurs forever** (Twilio $0.0005/min/month). Long retention compounds.
5. **AMD is per-call, and "premium" is 3× standard.** Easy to over-provision.
6. **Bandwidth's 10DLC campaigns carry a 3-month minimum.** Rarely mentioned outside support docs.
7. **Toll-free verification fees are unconfirmed at every vendor.** A real budget unknown.
8. **Port-in fees are unpublished everywhere.** Assume non-zero.
9. **Number purchase vs monthly fees get conflated.** Don't assume $0 acquisition.
10. **STT choice swings 18× inside a single vendor** (Telnyx Parakeet $0.0015 vs Azure $0.027).
    Accepting the default can wreck a voice-AI cost model.
11. **Several public pricing pages are marketing floors** — Bandwidth, Infobip, Vonage,
    SignalWire. A model built from them will be wrong.

## Sources
bandwidth.com/pricing · bandwidth.com/support/en/articles/12823178-carrier-surcharges ·
bandwidth.com/blog/10dlc-pricing-tiers · telnyx.com/pricing/{messaging,voice-api,elastic-sip,numbers} ·
twilio.com/en-us/{sms,voice}/pricing/us · plivo.com/{sms,voice}/pricing/us ·
sinch.com/pricing/sms · signalwire.com/pricing · infobip.com/pricing (no rates published).
Vonage and Bird primary pages 403'd — their figures are aggregator-sourced and marked `[U]`.
