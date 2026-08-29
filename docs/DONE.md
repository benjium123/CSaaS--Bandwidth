# DONE — all fourteen phases code-complete and deployed

**Date:** 2026-08-29 · **HEAD:** `90c47c1` · **Live:** https://csaas.sabinepropertygroup.net

## What was built (P11–P14, this cycle — P0–P10 were already live)

- **P11 — Outbound engine**: contact-list upload (CSV/XLSX, column mapping, per-row
  outcome report, DNC scrub, size caps), the **auto-texter** (campaigns with per-number
  rate+jitter pacing, trailing-26h daily caps, new-number warm-up ramp, retries with
  backoff, per-row message text from the sheet, every send through the full compliance
  gate, crash-safe exactly-once sends), and the **auto-dialer**
  (preview/power/parallel/predictive with the 3% abandon cap, AMD handling, compliance
  pre-dial checks, parallel losing-leg hangup). Lists + Campaigns console pages.
- **P12 — IVR/queues/voicemail/supervisor**: versioned call flows (menu/hours/
  ring-group/queue/voicemail/speak/transfer/hangup nodes, validation gate, in-flight
  pinning), carrier + room executors replacing the P6 inbound stub (idempotent on
  webhook redelivery, bounded, honest about what each path can do), ring groups,
  queues with first-answer-wins claims and overflow, voicemail with a Deepgram
  transcription seam, supervisor monitor/barge (whisper honestly FeatureUnavailable
  until the softphone client can enforce it). Flows + Queues console pages.
- **P13 — Platform services**: scoped rotatable API keys (same routes, hash-only),
  durable-outbox signed outbound webhooks (per-endpoint fan-out, backoff→dead,
  auto-disable, replay-deduped redelivery, SSRF-guarded), usage metering with a
  carrier reconciliation report (THE GATE), audit log, transcript search, LLM
  sentiment/call-scoring seam, analytics overview. Dashboard + Platform console pages.
- **P14 — Hardening**: killed-credentials SMS failover proven end-to-end
  (mutation-verified gate test; voice deferred by DR-2 amendment — OPEN_ISSUES D28),
  backups + restore drill (EXECUTED on the box, passed), public /status, number
  reputation monitoring, load-test tooling, RUNBOOK, security review (one MEDIUM →
  D26 rate limiting).

**Tests:** 911 backend + 66 frontend, ruff clean, OpenAPI drift gate green.
**Migrations:** 0012–0015 applied to production.

## Known limitations / where the truth lives

- `docs/OPEN_ISSUES.md` — the complete cleanup ledger (external blockers E1–E3,
  infra I1–I3, code C-rows, deferred findings D1–D29). Cleanup pass starts at **D26**.
- `docs/SECURITY_REVIEW_P14.md` — findings and dispositions.
- `docs/RUNBOOK.md` — ops procedures, executed drill results, the one remaining
  nginx operator step (exposes /status + the softphone WS).
- Live gates still blocked on external inputs only: **B1** (Telnyx keys), **B2**
  (Bandwidth SIP peer → the box), **B4** (AI keys) + the nginx step (I1). Every
  runtime seam behind them is built, tested against fakes, and honest when
  unconfigured.

## Next steps

1. User: B1/B2/B4 + the RUNBOOK nginx step → then the live gates (P1b SMS round-trip,
   P5–P9 voice/agent, P11 live campaign + dialer ear-tests, P12 IVR call).
2. Cleanup pass over OPEN_ISSUES, starting with D26 (login/TOTP rate limiting).
3. Agents-worker service on the box (I2) once B4 exists.
