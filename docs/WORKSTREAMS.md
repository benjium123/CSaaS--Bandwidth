# Workstreams

A **workstream** is a long-lived lane of ownership: a set of modules, a set of files, and a
set of invariants. A **phase** is a vertical slice that cuts across several workstreams and
ships something testable end-to-end.

Workstreams are how we avoid two agents editing the same file. Phases are how we avoid
building a database layer for three weeks with nothing to demo.

| ID | Workstream | Owns | Primary phases |
|---|---|---|---|
| **WS-0** | **Foundation** | config, settings validation, DB models, Alembic, auth, orgs/users/roles/RBAC, multi-tenancy, logging, error taxonomy | P0, ongoing |
| **WS-1** | **Carrier Abstraction Layer (CAL)** | `providers/` — the provider interface, Bandwidth adapter, Telnyx adapter, webhook ingest + signature verify + idempotency, number management, failover policy | P1, P4, P5, P14 |
| **WS-2** | **Messaging** | SMS/MMS send + receive, message/thread models, DLR state machine, media pipeline, templates + merge fields, link shortening | P1, P2, P3 |
| **WS-3** | **Voice Core** | call + leg state machines, BXML builder, command translation, recording, transfer, conference, IVR, queues, voicemail, AMD | P5, P6, P12 |
| **WS-4** | **Media & AI Voice Agent** | WS media server, frame codec + pacing, Pipecat pipeline, STT/TTS/LLM adapters, VAD/endpointing, barge-in, function calling, transcripts | P7, P8, P9 |
| **WS-5** | **AI SMS Agent** | conversation brain, state machine, per-thread memory, handoff rules, shared tool layer with WS-4 | P10 |
| **WS-6** | **Outbound Engine** | list upload/import, dedupe + validation, campaign builder, SMS scheduler + pacing + warm-up, auto-dialer (preview/power/parallel/predictive), dispositions | P11 |
| **WS-7** | **Console (frontend)** | React app — inbox, softphone, contacts, numbers, campaigns, dialer, analytics, admin | P2, P6, P11, P13 |
| **WS-8** | **Compliance & Deliverability** | consent ledger, opt-out engine, quiet hours, DNC, 10DLC + TFV registration, recording consent, number reputation | P3, P4, P14 |
| **WS-9** | **Platform Services** | usage metering, billing, public REST API, outbound webhooks, API keys, audit logs | P13 |
| **WS-10** | **DevOps** | docker-compose, nginx, systemd, CI, deploy scripts, backups, monitoring, runbook | P0, P14, ongoing |

## Cross-workstream invariants

These are owned by nobody and enforced by everybody. A PR that breaks one is rejected
regardless of which workstream it came from.

1. **Nothing calls a carrier SDK directly except WS-1.** WS-2/3/4/6 talk to the CAL interface.
2. **Nothing sends a message or places a call without passing through the compliance gate**
   (WS-8). The gate lives in the send path, not in each caller.
3. **Every webhook handler is idempotent and state-based.** See ARCHITECTURE D6.
4. **Audio changes must pass the conversation-replay test.** See ARCHITECTURE D5.
5. **No secrets in code.** Everything through `.env` → validated settings object.
6. **Every tenant-scoped query filters by `org_id`.** No exceptions, enforced at the
   repository layer, not remembered per query.
