# Roadmap (execution order, Fable 2026-09-10)

One phase at a time. Each phase: Fable schema + handoff → DeepSeek V4 Pro/Flash drafts under an
Opus supervisor (Opus reviews the draft, sends fixes back, stages the result) → Sonnet
integrator applies + runs suites → Opus verification → Fable sign-off → commit → deploy →
`/status` ok + two-way-audio smoke. Simplicity rule for every phase: the Work surface stays
Inbox / Contacts / Calls / Campaigns / Reports / Settings; each phase removes or merges at
least as much UI as it adds.

| # | Phase | Plan | Migration | Status |
|---|---|---|---|---|
| 1 | P22 Contact ownership, visibility, custom roles | docs/PLAN_P22_P25.md | 0024 | DEPLOYED 2026-09-10 (060b087) |
| 2 | P20 Simplicity: two-surface product | docs/phase-20-plan.md | – | P20a DEPLOYED (8a5b61e); P20 COMPLETE — P20a/b/c DEPLOYED 2026-09-10 (22e2a4d) |
| 3 | P21 Smart routing | docs/phase-21-plan.md | 0039 | DEPLOYED 2026-09-10 (b0f02d3; D43 campaign routing → P28) |
| 4 | P23a AI assistant: providers + builder + simulate | docs/PLAN_P22_P25.md | 0025 | DEPLOYED 2026-09-11 (6fa42a7) |
| 5 | P23b AI assistant: inbound/outbound wiring + outcomes | docs/PLAN_P22_P25.md | 0025 | queued |
| 6 | P24 AI metering, credits, Stripe (Fable owns ledger) | docs/PLAN_P22_P25.md | 0026 (schema + ledger service committed fb03825) | queued behind P23b |
| 7 | P25 Enterprise identity: sessions, 2FA policy, IP allowlist, OIDC | docs/PLAN_P22_P25.md | 0027 (schema committed 4f2ef3b) | queued |
| 8 | P26 Inbox pro: notes, mentions, "/" quick replies, snooze, SLA | docs/PLAN_P26_P36.md | 0028 | DEPLOYED 2026-09-11 |
| 9 | P27 Contacts pro: export, merge, saved views, retention, erasure | docs/PLAN_P26_P36.md | 0029 (schema committed a521370) | queued |
| 10 | P28 Messaging: MMS, send-later, link tracking, plain failure reasons | docs/PLAN_P26_P36.md | 0030 (schema committed) | queued |
| 11 | P29 Voice: coaching enforcement, dual recording, consent, failover, dispositions | docs/PLAN_P26_P36.md | 0031 (schema committed) | queued |
| 12 | P30 Reports: team, SLA, campaigns, assistant; export; email; wallboard | docs/PLAN_P26_P36.md | 0032 (schema committed) | queued |
| 13 | P31 Notifications, PWA, mobile | docs/PLAN_P26_P36.md | 0033 | queued |
| 14 | P32 Plans, traffic billing, invoices (Fable owns money) | docs/PLAN_P26_P36.md | 0034 | queued |
| 15 | P33 Agencies: sub-accounts, white-label, custom domain | docs/PLAN_P26_P36.md | 0035 | queued |
| 16 | P34 Developers and integrations: API docs, Zapier, HubSpot, Salesforce | docs/PLAN_P26_P36.md | 0036 | queued |
| 17 | P35 Channels: email, WhatsApp, web chat | docs/PLAN_P26_P36.md | 0037 | queued |
| 18 | P36 Trust: PII encryption at rest, status history, i18n | docs/PLAN_P26_P36.md | 0038 | queued |

External inputs still owed by the operator: valid Bandwidth API credentials + application
callbacks (see session notes 2026-09-09), Bandwidth account upgrade (trial 402), Stripe keys
(P24), VAPID keys (P31), OIDC client (P25).
