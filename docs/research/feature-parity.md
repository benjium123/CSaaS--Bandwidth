# Research — CPaaS Feature Parity Checklist (2026)

Surveyed: Twilio, Bandwidth, Telnyx, Vonage, Plivo, Bird, Sinch, RingCentral, Dialpad,
Aircall, JustCall, OpenPhone, Kixie, Close, GoHighLevel LC Phone, Podium, Vapi, Retell,
Bland, Synthflow, ElevenLabs Agents.

`TS` = table stakes · `DIFF` = differentiator · `NTH` = nice to have

---

## 1. Messaging
| Feature | | Best-in-class |
|---|---|---|
| SMS send/receive (long code, short code) | TS | Twilio |
| MMS | TS | Bandwidth |
| Group MMS | NTH | Sinch |
| RCS (cards, carousels, suggested replies, typing/read receipts) | DIFF→TS | Twilio Studio, Sinch |
| WhatsApp Business API | DIFF | Bird |
| iMessage for Business | NTH | Sendblue |
| Scheduled send | TS | JustCall |
| Bulk / campaign send with pacing | TS | Bird |
| Templates + merge fields | TS | GHL |
| Link shortening + per-recipient click tracking | DIFF | Attentive, Podium |
| Delivery receipts w/ carrier error codes | TS | Twilio |
| Inbound routing (keyword / number / hours) | TS | Twilio Studio |
| Auto-replies | TS | Podium missed-call-text-back |
| STOP / HELP / START handling | TS (legally required) | Twilio |
| Quiet hours, timezone-aware per recipient | DIFF | Kixie |
| Throttling / pacing / ramp-up | DIFF | Twilio Messaging Service |
| Number pools + sticky sender | DIFF | Twilio Messaging Service |
| Message tagging | NTH | JustCall |
| Media handling + transcoding | TS | Bandwidth |
| Unified conversation threading | DIFF | Twilio Conversations |
| Drip sequences | DIFF | JustCall Sequences |

## 2. Voice
| Feature | | Best-in-class |
|---|---|---|
| Outbound dial / click-to-call | TS | all |
| Inbound routing / ACD | TS | RingCentral, TaskRouter |
| IVR menus (DTMF + speech, multi-level) | TS | Twilio Studio |
| Call queues + hold music | TS | RingCentral |
| Ring groups, simultaneous ring, sequential/find-me | TS | RingCentral |
| Warm transfer (announced) | TS | Aircall |
| Cold/blind transfer | TS | all |
| Conference / 3-way | TS | Twilio Conference API |
| Call recording | TS | all |
| — dual-channel (separate agent/customer tracks) | DIFF | Twilio — required for QA scoring |
| — pause/resume for PCI redaction | DIFF | Talkdesk/Five9 |
| Voicemail | TS | all |
| Voicemail drop (pre-recorded, skips ring) | DIFF | Kixie, JustCall |
| Voicemail transcription | TS | Dialpad (bundled) |
| Whisper / barge / monitor | DIFF | Kixie |
| Power dialer (sequential auto-advance) | DIFF | JustCall, Kixie |
| Parallel/multi-line dialer (N lines, first answer wins) | DIFF | Kixie (up to 10 lines) |
| Predictive dialer (abandon-rate pacing) | DIFF | ViciDial-class |
| Local presence caller ID | DIFF | Kixie |
| Answering machine detection | DIFF | Twilio AMD |
| DTMF capture | TS | Twilio `<Gather>` |
| SIP trunking / BYOC | DIFF | Telnyx |
| E911 | TS (regulatory) | Bandwidth |
| Business-hours + holiday routing | TS | Aircall |
| Callback queuing ("keep your place in line") | DIFF | RingCentral CX |
| Call disposition codes | TS | Kixie, Close |
| After-call-work timer | NTH | Dialpad |

## 3. AI
| Feature | | Best-in-class |
|---|---|---|
| Realtime voice agent (full duplex, barge-in) | DIFF | Retell (~600ms), ElevenLabs (voice realism) |
| AI receptionist | DIFF | RingCentral, Podium AI Employee |
| AI SMS agent | DIFF | Podium, GHL |
| Live transcription | TS | Dialpad (bundled at entry tier) |
| Post-call transcript + summary | TS | RingCentral ACE |
| Sentiment analysis | DIFF | RingCentral ACE |
| Call scoring / QA scorecards | DIFF | JustCall |
| Coaching / next-best-action | DIFF | RingCentral ACE |
| Keyword / topic detection | NTH | Dialpad |
| Knowledge base / RAG grounding | DIFF | Bland, Vapi |
| Function/tool calling (book, CRM lookup, transfer) | DIFF (the real moat) | Vapi |
| Multi-language | DIFF | ElevenLabs, Sinch |
| Voice cloning | NTH | ElevenLabs |
| AI voicemail detection + handoff | DIFF | Bland |
| Conversation-intelligence dashboards | DIFF | Dialpad, Gong-class |

**2026 market read:** dev-infra players (Vapi, Bland, LiveKit) win on custom logic;
productized agents (Retell, Synthflow) win on time-to-production; ElevenLabs wins on
pure voice quality for structured flows.

## 4. Contacts / CRM
Contacts + companies `TS` · custom fields `TS` · tags `TS` · static + dynamic segments `TS` ·
CSV import/export `TS` · **dedupe/merge `DIFF` (usually half-built)** · unified activity
timeline `DIFF` · notes `TS` · tasks/reminders `TS` · two-way CRM sync `DIFF` (JustCall,
Aircall) · **do-not-contact / suppression lists `TS` — where homegrown tools most often fail**

## 5. Platform / Admin
Multi-tenant orgs & sub-accounts `DIFF` · users/teams `TS` · granular RBAC `DIFF` ·
number inventory & assignment `TS` · porting `TS` · **10DLC + toll-free verification
workflow `TS` (mandatory)** · audit logs `DIFF` · scoped rotatable API keys `TS` ·
webhooks + event subscriptions `TS` · documented rate limits `TS` · usage metering +
invoicing `TS` · cost-per-call/message analytics `DIFF` · SSO/SAML `DIFF` · 2FA `TS` ·
retention policies `DIFF` · GDPR/CCPA deletion `DIFF` · **recording consent by state
`DIFF` — legally load-bearing, no CPaaS automates it, you must build it** · TCPA tooling
`TS` · DNC scrubbing `TS` · spam-label / STIR-SHAKEN reputation monitoring `DIFF` ·
carrier failover `DIFF` · geo-redundancy `DIFF` · status page `TS`

> 2026 change: **MMS/SMS throughput is now driven by Brand Trust Score + campaign use
> case, not a flat 1 MPS cap.** Code written against the old fixed limits will mis-send.

> Two-party consent: if **either** party is physically in a two-party-consent state, the
> whole call needs consent. **Area code ≠ physical location.** ~12 states (CA, FL, IL, MD,
> MA, MI, MT, NV, NH, PA, WA, +).

## 6. UX surfaces
Unified inbox `DIFF` · browser softphone widget `TS` · mobile apps `TS` (OpenPhone sets
the bar) · click-to-dial browser extension `DIFF` (Aircall) · desktop app `NTH` ·
dashboards/reports `TS` · real-time wallboard `DIFF` · scheduled emailed reports `NTH` ·
push notifications `TS`

## 7. Developer
REST API `TS` · SDKs `TS` · **webhook signature verification `TS`** · sandbox/test creds
`TS` · **idempotency keys `DIFF` — always missing until you get burned** · cursor
pagination `TS` · documented error taxonomy `DIFF` (Twilio is the gold standard)

---

## THE 18 THINGS THAT BITE IN PRODUCTION

1. **STOP across number pools.** A STOP to number A must suppress B, C, D. Per-number
   opt-out lists are a bug, not a feature.
2. **Timezone-correct quiet hours** must resolve to the *recipient's* timezone, not the
   server's. DST breaks naive implementations twice a year.
3. **MMS size/type limits.** Carriers silently downres; PDFs and odd formats just fail.
   Confirm delivery, never assume.
4. **Carrier content filtering.** URL shorteners, spam keywords and unregistered campaigns
   get filtered *while reporting "delivered"*. The DLR lies.
5. **10DLC throughput is dynamic** (Trust Score based) as of Mar 2026.
6. **Toll-free verification is a separate track from 10DLC.** 3–6 weeks, different
   failure modes. Brand approval does not cover toll-free.
7. **Recording storage cost at scale.** Dual-channel + long retention is a real line item.
   Plan lifecycle tiering on day one.
8. **Webhook replay / idempotency.** Providers retry on non-2xx. Without idempotency keys
   a transient 500 double-charges and double-notifies.
9. **Call-leg state machines.** A "call" is N legs. Status callbacks fire per leg. Code
   that assumes one event per call breaks on transfer, conference and AMD.
10. **DTMF loss in transcoding.** Across SIP↔WebRTC↔PSTN or through an AI pipeline,
    in-band tones get mangled. Use RFC 2833 / SIP INFO / gather APIs. Never detect tones
    from transcoded audio.
11. **AMD false-positive vs latency tradeoff.** AMD adds 1–3s and misclassifies fast
    talkers as voicemail. Directly moves your connect-rate metric.
12. **Two-party consent across state lines** (see above).
13. **A2P vs P2P classification drift.** Valid 10DLC registration does not protect you if
    your volume *pattern* reads as P2P-at-scale.
14. **Number reputation is dynamic.** A clean number gets "Spam Likely" mid-campaign from
    complaint volume. Needs active monitoring + rotation, not one-time setup.
15. **Local presence ≠ compliance.** Area-code matching lifts pickup but regulators have
    specifically targeted the pattern.
16. **Voicemail drop timing** depends on beep detection; drift clips the message start and
    it is carrier/handset dependent.
17. **Conference recording consent multiplies** — every added leg is a new party.
18. **Sticky-sender pool exhaustion.** Under load, conversations silently jump sender
    numbers mid-thread, breaking continuity and confusing recipients.
