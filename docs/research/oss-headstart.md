# Research — Open-source headstart: what to fork, harvest, or depend on

All stars / last-push / license verified against the GitHub API on 2026-08-26, not from
search summaries.

## TOP 8 — the ones we actually use

| # | Repo | What we take | Mode | License risk |
|---|---|---|---|---|
| 1 | **fastapi/full-stack-fastapi-template** (45,158★, pushed 2026-08-25) | Project skeleton — FastAPI + React + Vite + Tailwind + shadcn/ui + Postgres + Docker Compose. Exact stack match. | **Fork as scaffold** | 🟢 MIT |
| 2 | **Bandwidth/pipecat-bandwidth** | Bandwidth ↔ Pipecat media serializer with `clear` barge-in. First-party. | **Dependency** | 🟢 BSD-2 |
| 3 | **pipecat-ai/pipecat** (~14k★) | The AI voice pipeline itself (STT/LLM/TTS orchestration, VAD, interruption, flows). | **Dependency** | 🟢 BSD-2 |
| 4 | **dograh-hq/dograh** (~5.5k★, daily commits) | Visual agent workflow builder + multi-provider telephony abstraction + dashboard patterns. Voice-only, no SMS/CRM. | **Harvest** | 🟢 BSD-2 |
| 5 | **onsip/SIP.js** (2,094★, pushed 2026-06-15) | Browser WebRTC↔SIP signaling, if/when we do our own softphone stack. No UI — a library, not a fork target. | **Dependency (later)** | 🟢 MIT |
| 6 | **daviddrysdale/python-phonenumbers** (3,766★, pushed 2026-08-14) | Number normalization / validation / line-type detection everywhere. | **Dependency** | 🟢 Apache-2.0 |
| 7 | **svix/svix-webhooks** | Outbound webhook delivery — retries w/ exponential backoff, durable replay, FIFO, Standard Webhooks compliant. Also the reference architecture for our *inbound* carrier-webhook idempotency. | **Dependency (sidecar)** | 🟢 MIT |
| 8 | **openmeterio/openmeter** | Real-time usage metering per tenant (SMS / call minutes / AI tokens) → billing. | **Dependency (later)** | 🟢 Apache-2.0 |

Also: **Bandwidth-Samples/openai-realtime-websockets-python** — the official
`<StartStream>` bidirectional protocol reference. Read it, don't fork it.

## Harvest, do not fork

- **chatwoot/chatwoot** (36,214★, daily activity, 🟢 MIT core / 🟡 `enterprise/` carve-out) —
  the single best unified-inbox reference. Already has SMS-via-Twilio and Voice-via-Twilio
  channels, contacts, canned responses, agent assignment, campaigns, labels, CSAT.
  **Rails + Vue, not our stack.** Take the data model (conversations / contacts / inboxes /
  labels) and the thread UX. Forking it means adopting Rails — only worth reconsidering if
  we decide stack purity is negotiable.
- **Wazo Platform** (🟡 GPL-3.0, fragmented across ~40 repos) — best *architecture*
  reference for SIP topology: API-first Python microservices over Kamailio + RTPengine +
  Asterisk, OpenAPI everywhere. Read it; don't run it.
- **jambonz** — best call-control API design reference. See license flag below.
- **FusionPBX** (🟡 MPL-1.1, weak file-level copyleft) — multi-tenant domain-based PBX admin
  UI patterns.
- **jsz-05/LLM-State-Machine** — the FSM-around-LLM pattern only. Not SMS-specific, no
  telephony, no dashboard.

## Rejected, with reasons

| Repo | Why not |
|---|---|
| **erxes** (4,074★) | **GPLv3 + Commons Clause.** Explicitly forbids charging others to host/support it — bans exactly this business model. 🔴 |
| **VICIdial** | **AGPLv2** (closes the ASP loophole) + Perl/Asterisk/MySQL from the 2000s. Harvest dialer-pacing *concepts* only. 🔴 for code. |
| **papercups** | **Dead** — last push Feb 2024, 2.5 years silent despite clean MIT. |
| **drachtio-cpaas-server** | **Dead** — last push 2018, 1★, self-described WIP. (Author later built jambonz.) |
| **FreeScout / Zammad / Lago** | AGPL-3.0. Fine to run unmodified as a sidecar; risky to fork-and-resell. 🟡 |
| **Vocode** | Dead (see ai-voice-stack.md). |
| **jambonz core v10+** | Repos read MIT, **but jambonz's own blog announces v10.x+ core requires a paid license key tied to your DNS domain**; only client SDKs / Node-RED plugin / npm libs stay MIT. **Verify the exact version tag's license before spending engineering time.** 🟡 |
| **Twilio quickstarts / TwiML libs** | Client libraries for *consuming* Twilio, not servers to fork. Useful only to study TwiML verb design when designing our own call-control DSL. |

## Two confirmed structural gaps — stop looking

Exhaustive search, not a search-quality failure:

1. **No OSS TCPA/DNC compliance library exists in any language.** Build in-house or buy an
   API (TCPA Litigator List, RealValidation). We already have hard-won prior art in the
   dispo stack — reuse those lessons (see the DNC-scrubbing-disabled incident that ate ~65%
   of a realtor list).
2. **No portable 10DLC / Campaign Registry automation library exists.** Only hit was
   `aws-samples/sample-sms-10dlc-registration-automation` — Step Functions/Lambda/DynamoDB,
   AWS-locked, not a library. Carrier SDKs have 10DLC *bindings*, not compliance tooling.

**Also: no production-grade OSS AI SMS agent exists.** Chatwoot/Tiledesk give the
human-handoff inbox half; the LLM decisioning half you write yourself. Our existing
`acquisition_brain` / negotiation-daemon pattern is arguably more mature than anything in
OSS for this niche — port those lessons rather than shopping.

## Supporting utilities
- **shlinkio/shlink** (5,245★, 🟢 MIT) — self-hosted URL shortener + click analytics for SMS
  campaign link tracking. PHP; run as a sidecar behind its REST API, don't port it.
- **Unleash** (🟢 Apache-2.0) or **Flagsmith** (🟢 BSD-3) — feature flags for multi-tenant rollout.
- **Transactional outbox** — no repo worth forking (`python-outbox` self-describes as
  educational). Implement directly: outbox table + poller into the queue.
- **Casbin** — has a FastAPI integration for RBAC/ReBAC/ABAC. Note
  `full-stack-fastapi-template` ships **only JWT auth + a superuser/user binary** — no
  multi-tenancy, no RBAC. We build that ourselves.
