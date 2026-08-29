# OPEN ISSUES — running ledger for the P11–P14 push

> Created 2026-08-29. Every issue found while executing P11–P14 that is NOT solved
> in-phase gets a row here, to be resolved in a dedicated cleanup pass at the end.
> When one is fixed, mark it ✅ with the commit. Do not silently drop rows.

## External inputs (user-only — cannot be coded around)

| # | Issue | Blocks | Unblock action |
|---|---|---|---|
| E1 | **B1: no messaging-capable carrier.** Telnyx keys absent from `/opt/csaas/.env`. | P1b, P4 registration, P10 live SMS turn, P11 live campaign gate | User pastes `TELNYX_API_KEY` + `TELNYX_MESSAGING_PROFILE_ID`, restarts api. 10DLC brand/campaign in Telnyx portal. |
| E2 | **B2: no SIP trunk points at the box.** | P5–P9 runtime gates, P11 dialer live gate, P12 live gate | Bandwidth portal: voice app → inbound SIP peer `144.126.152.175:5060`, assign `+19404060664`. Port is open and listening (verified 2026-08-29). |
| E3 | **B4: no AI provider keys in production.** `ANTHROPIC_API_KEY` / `DEEPGRAM_API_KEY` / `ELEVENLABS_API_KEY` empty. | P8/P9 voice agent, P10 SMS agent LLM turn | User pastes keys into `/opt/csaas/.env`, restarts api. |

## Infrastructure gaps (code/config work, deferred deliberately)

| # | Issue | Found | Notes |
|---|---|---|---|
| I1 | **nginx has no wss proxy for LiveKit 7880** — browser softphone cannot connect. | 2026-08-29 B3 bring-up | Additive `location` block on the csaas nginx site + `nginx -t` + reload. Deferred because nginx is shared with other tenants; needs explicit go-ahead for the reload. |
| I2 | **No agents-worker service on the VPS.** `agents/` code is shipped but has no venv or systemd/compose unit. | 2026-08-29 | Needs Python 3.11+ venv on the box + a unit. Pointless before E2/E3. |
| I3 | **`deploy/livekit/README.md` and `sip.yaml` comments document a TELNYX trunk** — voice is Bandwidth now (D-split at P3). | 2026-08-29 | Doc fix; step 5 of the README must not be followed as written. |

## Code issues (carried from earlier phases)

| # | Issue | Found | Notes |
|---|---|---|---|
| C1 | **`test_softphone_token_cross_org_room_is_404` flakes on CI Python 3.10 only.** Second failure signature was auth (`Incorrect email or password` from `register_and_login`), not the dial-task race. Suspect cross-test state on 3.10. | pre-2026-08-29 | Reproduce with the FULL suite under 3.10, not the file alone. |
| C2 | **Local dev SQLite runs the app with FKs unenforced** (pragma only set on the test engine). Prod is Postgres — dev-fidelity gap only. | P2a | |
| C3 | **R2 unmeasured:** VPS ↔ Bandwidth media PoP may be long-haul → TCP dead-air risk. Measurement scripts exist (`measure/`), need a live trunk. | planning | Run at P7 gate time (needs E2). |
| C4 | **R3 unmeasured:** Deepgram Nova-3 real WER on 8 kHz caller audio. | planning | P8 bake-off (needs E2+E3). |

## Discovered during P11–P14 (append below as found)

| # | Issue | Found | Notes |
|---|---|---|---|
| D1 | `pacing.predictive_coefficient` floor=0.25 overrides the 3% abandon target when observed abandon > 12%, and `parallel_lines=1` predictive campaigns can never throttle below 1 line — docstring overclaims a guarantee. | P11 Opus review | Tighten floor semantics or document honestly. |
| D2 | Predictive denominator counts compliance-blocked rows (never dialed) as "placed", understating abandon rate. | P11 Opus review | Exclude disposition="blocked" from placed. |
| D3 | `DIAL_STATUSES` lacks `blocked`; compliance refusals ship as `failed` + disposition="blocked". | P11 Opus review | Add `blocked` in the next migration touching outbound. |
| D4 | Uploaded list source blob (`org/{org}/imports/{list}/source`) is never deleted and has no retention. | P11 Opus review | Delete after import completes, or add to media purge sweep. |
| D5 | `SEND_TERMINAL` includes `deferred`: a campaign reads "completed" while gate-held sends are still awaiting sweeper release. | P11 Opus review | Progress endpoint should surface held count; consider completing only when releases resolve. |
| D6 | Campaign pause is honored between ticks only — a pause mid-tick still sends the remaining batch (max `OUTBOUND_TICK_BATCH`). | P11 Opus review | Re-check status inside the send loop if this matters at larger batch sizes. |
| D7 | Dial tick batch budget charges deferred/compliance-blocked rows though no dial was placed. | P11 Opus review | Cosmetic throughput loss. |
| D8 | No RBAC deny tests for `/api/v1/outbound/*` (generic deny path covered elsewhere). | P11 Opus review | Coverage hygiene. |
| D9 | `allow_unscoped` uses in outbound.py/dialer.py lack the inline justification comment style db/base.py mandates. | P11 Opus review | Move rationale inline. |
| D10 | `resolve_or_create_contact`'s IntegrityError path rolls back the whole session — inside `run_import`'s 200-row batches a genuine phone race discards the current batch. | P11 implementer | Narrow to a nested savepoint. |
| D11 | List import re-checks DNC/opt-out per row (two queries/row) — fine at 500 rows, quadratic pain at 50k. | P11 implementer | Batch the scrub queries. |
| D12 | Campaign throughput is bounded at ~1 send/number/tick (one frozen `now` per tick) — high-volume orgs need a short sweeper interval, not bigger batches. | P11 implementer | Document in ops runbook (P14). |
| D13 | Voice-agent (LiveKit worker) token usage is not reported to the backend — `ai_tokens` metering covers SMS turns only. | P13 planning | Extend the worker transcript seam to carry usage. |
| D14 | Voice campaign.completed / voicemail hold events: `campaign.completed` outbox hook lands with the P11 fix round; `voicemail.created` must be wired inside P12's services/voicemail.py. | P13 planning | Verify both hooks exist before P13 webhook tests rely on them. |
