# SPEC — CSaaS (Communications Software as a Solution)

A self-hosted, multi-tenant communications platform: SMS, MMS, voice, WebRTC softphone,
contacts, unified inbox, campaigns, auto-dialer, and AI agents on **both** SMS and voice.
Carrier: **Bandwidth primary, Telnyx failover.**

**The end goal that everything serves:** AI-driven conversations on SMS and calls, with a
human able to take over at any point, and full compliance and auditability throughout.

---

## Scope → phase map

Every feature below is assigned to a phase. `—` means explicitly out of scope for v1 with a
stated reason. Derived from the 2026 CPaaS parity survey in `docs/research/feature-parity.md`.

### Messaging
| Feature | Phase |
|---|---|
| SMS send/receive | P1 |
| Conversation threading | P2 |
| Sticky sender | P2 |
| MMS + media pipeline | P3 |
| Templates + merge fields | P3 |
| STOP/HELP/START across the whole number pool | P3 |
| Quiet hours in recipient timezone | P3 |
| DNC + consent ledger | P3 |
| Delivery receipts + carrier error taxonomy | P1 |
| Scheduled send | P11 |
| Bulk campaign send with pacing + warm-up | P11 |
| Number pools | P2 / P11 |
| Link shortening + click tracking | P13 (Shlink sidecar) |
| Drip sequences | P11 |
| Message tagging | P2 |
| **RCS** | **— out of v1.** Bandwidth A2P RCS at scale is "coming soon". |
| **WhatsApp / iMessage** | **— out of v1.** Different channel model; revisit post-P14. |

### Voice
| Feature | Phase |
|---|---|
| Outbound dial / click-to-call | P5 |
| Inbound routing | P5 → P12 |
| Call + leg state machine | P5 |
| Recording (+ storage lifecycle) | P5 |
| Blind transfer, DTMF gather | P5 |
| AMD | P5 (wired), P11 (tuned) |
| Browser softphone, per-call caller ID | P6 |
| Hold / mute / device selection | P6 |
| Warm transfer | P9 (AI→human), P12 (human→human) |
| IVR (DTMF + speech) | P12 |
| Queues + hold music + callback queuing | P12 |
| Ring groups, simultaneous/sequential ring | P12 |
| Business hours + holiday routing | P12 |
| Voicemail + transcription | P12 |
| Voicemail drop | P9 |
| Whisper / barge / monitor | P12 |
| Conference / 3-way | P12 |
| Call disposition codes + wrap-up | P11 |
| Dual-channel recording | P13 |
| **PCI pause/resume redaction** | **— out of v1.** No card capture in scope. |
| **E911** | **— out of v1.** Not a phone-system replacement; revisit if we sell seats. |

### AI
| Feature | Phase |
|---|---|
| Real-time voice agent, barge-in | P8 |
| Live transcription | P7/P8 |
| Function/tool calling | P9 |
| Warm handoff to human with context | P9 |
| Knowledge base / RAG grounding | P9 |
| AI SMS agent + state machine + handoff | P10 |
| Post-call summary | P13 |
| Sentiment + call scoring | P13 |
| Conversation intelligence dashboard | P13 |
| **Voice cloning** | **— out of v1.** Use stock voices. |
| **Multi-language** | **— out of v1.** English only; the pipeline is language-pluggable. |

### Contacts / CRM
| Feature | Phase |
|---|---|
| Contacts, companies, custom fields, tags, notes | P2 |
| Unified activity timeline | P2 → P5 (calls) |
| Static + dynamic segments | P11 |
| Import/export + column mapping | P11 |
| Dedupe / merge | P11 |
| Do-not-contact / suppression | P3 |
| Tasks / reminders | P13 |
| **External CRM sync** (HubSpot/Salesforce/GHL) | **— out of v1.** Public API in P13 makes it buildable. |

### Outbound engine
| Feature | Phase |
|---|---|
| List upload (CSV/XLSX) + validation + DNC scrub on import | P11 |
| Campaign builder | P11 |
| SMS scheduler + per-number pacing + jitter + daily caps | P11 |
| New-number warm-up ramp | P11 |
| Preview / power dialer | P11 |
| Parallel dialer (N lines, first answer wins) | P11 |
| Predictive dialer with abandon-rate pacing (≤3%) | P11 |
| Retry backoff schedules | P11 |
| Local presence caller ID | P11 (**off by default** — regulators target the pattern) |

### Platform / admin
| Feature | Phase |
|---|---|
| Multi-tenant orgs, users, teams | P0 |
| Granular RBAC | P0 |
| Number inventory, search, order, release, configure | P4 |
| Port-in with LNP check | P4 |
| 10DLC brand + campaign registration | P4 |
| Toll-free verification | P4 |
| Recording consent by state | P3 (policy) / P5 (enforcement) |
| Number reputation monitoring | P14 |
| Carrier failover | P14 |
| Usage metering + cost analytics | P13 |
| Public REST API + outbound webhooks | P13 |
| Scoped rotatable API keys | P13 |
| Audit logs | P13 |
| Backups + restore drill | P14 |
| Status page / health | P14 |
| **SSO/SAML** | **— out of v1.** 2FA in P0; SAML when we have an enterprise buyer. |
| **Billing / invoicing** | **— out of v1.** Metering ships P13; invoicing is a business decision. |

### UX surfaces
| Feature | Phase |
|---|---|
| Unified inbox | P2 |
| Browser softphone widget | P6 |
| Dialer UI | P11 |
| Dashboards + reports | P13 |
| Notifications | P13 |
| **Mobile apps** | **— out of v1.** PWA first; native when seat count justifies it. |
| **Browser extension / desktop app** | **— out of v1.** |

---

## Non-functional requirements

| | Target |
|---|---|
| AI voice-to-voice latency | **p50 ≤ 700 ms, p95 ≤ 1100 ms** (stretch: sub-500 ms with co-location) |
| Webhook ack | 2xx in **< 2 s**, work done async |
| Webhook processing | **idempotent + state-based**, survives 3× out-of-order replay |
| Tenant isolation | enforced at the repository layer, tested — not remembered per query |
| Audio | conversation-replay test green before any audio deploy |
| Media transport | WS/TCP leg **same-region only**; measured before P8 |
| Secrets | `.env` only, validated at boot, encrypted at rest in the DB |
| Recording retention | configurable, default 90 d; transcripts 365 d |

## Definition of done for the whole project
All 15 phase gates passed, deployed to the VPS, failover drill passed, restore-from-backup
drill passed, and a `DONE.md` written covering what was built, known limitations, and next steps.
