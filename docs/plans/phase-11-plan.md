# Phase 11 — Outbound engine: list upload, SMS campaign scheduler, auto-dialer

**WS:** 6, 7 · **Deploy:** yes · **Planned:** 2026-08-29 (Fable)

## Goal

An org can upload a contact list (CSV/XLSX with column mapping and a per-row outcome
report), build an SMS campaign against it that runs to completion under the full
compliance gate with per-number pacing/caps/warm-up, and run a dialer campaign
(preview/power/parallel/predictive) that places calls through the LiveKit voice plane.
The live-human half of the dialer gate is blocked on B2 (no SIP trunk) — it is built,
tested against fakes, and recorded as runtime-blocked, exactly like P5–P9.

## Design rulings (settled — do not relitigate in implementation)

- **DR-1: Names.** The registration model already owns `Campaign` (10DLC). Outbound
  tables are `contact_lists`, `contact_list_rows`, `outbound_campaigns`,
  `outbound_sends`, `dial_attempts`. API namespace: `/api/v1/outbound/*`.
- **DR-2: One send path.** Every campaign SMS goes through
  `services/messaging.send_message` and therefore the full compliance gate (opt-out →
  DNC → quiet hours, defer/hold). The campaign layer may pre-filter known-DNC rows to
  save work but the GATE is the authority — no duplicated compliance logic, no second
  send path (mirrors P10 DR-4).
- **DR-3: `BULK_SEND_KEY` fixes the recorded P10 deferral.** Campaign sends set
  `session.info[BULK_SEND_KEY] = True`; the `handed_off` flip in `send_message` treats
  it exactly like `AI_SEND_KEY` (a bulk send is not a human takeover). This is the ONLY
  edit to `messaging.py`, made by Fable (Tier 1) — implementers do not touch that file.
- **DR-4: Crash-safe, idempotent sends.** `outbound_sends` has UNIQUE
  `(campaign_id, e164)`. The scheduler claims a row (`queued` → `sending`), calls
  `send_message`, and links `message_id` + flips to `sent`/`deferred`/`blocked` in the
  SAME session/commit — a crash cannot double-send. On startup/tick, `sending` rows with
  no `message_id` older than 5 min are re-queued.
- **DR-5: Pacing is DERIVED, never counted** (P2a lesson). Per-number "daily" cap =
  `COUNT(messages)` for that `from_e164` over a TRAILING `CAP_WINDOW_HOURS` (26h)
  window — AMENDED at review time from UTC-calendar-day: a rolling window has no
  midnight burst, matches carrier warm-up reality, and stays correct under a
  test-frozen tick clock sitting ahead of DB-real timestamps. Recipient-local fairness
  comes from the gate's quiet hours. Warm-up ramp: `org_numbers.warmup_started_at` (new column, nullable = no
  ramp); ramp schedule lives in `services/pacing.py` as data
  (`[(3, 50), (7, 100), (14, 250)]` → else uncapped by ramp), effective cap =
  `min(campaign.daily_cap, ramp_cap)` when `respect_warmup`.
- **DR-6: The scheduler is a tick inside the EXISTING sweeper loop** — no new process,
  no Redis, no new loop. `sweeper.run_once` gains `outbound_tick` and `dialer_tick`.
  Tests call the tick functions directly, never the loop (existing convention). The
  Redis bus swap stays deferred (noted in OPEN_ISSUES if P14 load-testing demands it).
- **DR-7: Send-rate jitter.** A campaign has `rate_per_minute` per sending number; the
  tick computes each number's earliest-next-send as `last_send + 60/rate ± 20%` jitter
  (seeded RNG in tests). A tick sends at most `OUTBOUND_TICK_BATCH` (default 25) rows.
- **DR-8: Import is two-step and openpyxl is APPROVED (new dep, Fable Tier-1 ruling).**
  Step 1: upload file → parse headers + first 5 rows → return preview + suggested
  mapping (fuzzy header match: phone/mobile/cell → phone, etc.). Step 2: commit with
  explicit mapping → background import task (same asyncio-task pattern as sms_agent,
  with a `wait_for_pending_import_tasks()` test hook). CSV via stdlib, XLSX via
  openpyxl (read-only mode). Per-row outcome stored on `contact_list_rows`:
  `accepted | invalid | duplicate | dnc` + human-readable reason.
- **DR-9: Import validation.** `phonenumbers` with region US: not parseable/possible →
  `invalid`; duplicate E.164 within the list → `duplicate` (first wins); internal DNC or
  latest consent = opted-out → `dnc` (kept, never deleted, still re-checked by the gate
  at send time). Line-type lookup is a SEAM (`line_type` nullable, populated only when a
  lookup-capable carrier is configured — Telnyx lookup needs B1; stub records `None`).
  Accepted rows upsert a `Contact` (match on E.164) and link `contact_id`.
- **DR-10: Dialer modes.** `preview` (agent explicitly launches each call), `power`
  (auto-dial next when agent idle, 1:1), `parallel` (N legs, first `connected` wins,
  siblings hung up), `predictive` (power pacing multiplied by a coefficient derived from
  the measured abandon rate, hard-capped so projected abandon ≤ 3% — FTC/TSR). AMD uses
  the existing P9 verdict seam (`amd_verdict` recorded; voicemail → disposition
  `voicemail`, optional voicemail-drop is P9's machinery, not re-implemented).
  `local_presence` defaults FALSE; when on, pick a from-number matching the contact's
  area code if the org pool has one, else fall back to normal selection — no number
  renting, no pattern beyond that (regulator-flagged feature, deliberately minimal).
- **DR-11: Calls respect compliance too.** Before dialing, the dialer runs the same
  primitives the gate uses: internal DNC check + quiet-hours check for the contact's
  region (defer `next_attempt_at` to the window edge) + opted-out consent check.
  Voice reuses the compliance module's functions — it does NOT get a parallel
  implementation.
- **DR-12: Retries.** SMS: carrier `failed` → exponential backoff
  (`retry_backoff_minutes * 2^attempt`), max `max_attempts` (default 2); `blocked`/DNC
  never retried. Dialer: `no_answer`/`busy`/`failed` retried up to `max_attempts`
  (default 2) spaced `retry_backoff_minutes` (default 240). All retry state on the row
  (`attempts`, `next_attempt_at`) — restart-safe.
- **DR-14: Per-row message text (user directive 2026-08-29 — "auto texter").** The
  uploaded sheet may carry a `message` column (mapped like any field, synonyms in
  `list_parsing.FIELD_SYNONYMS`). Extracted canonical fields live on
  `contact_list_rows.fields`. At send time the body resolves in this order:
  campaign `body` rendered with the row's fields as `{{merge}}` context (existing P3
  template renderer) → if campaign body is empty, the row's `message` field verbatim →
  if both empty, the row is `skipped` with reason "no message". A campaign is startable
  when it has a body OR its list has per-row messages.
- **DR-13: The dial seam is injectable.** `services/dialer.py` calls
  `voice_plane.service.start_room_call` through a module-level indirection so tests
  drive a fake that resolves legs to `connected`/`no_answer`/`voicemail`
  deterministically. No LiveKit dependency in unit tests.

## Schema (Tier-1, done by Fable — implementers do not touch)

Migration `0012_outbound_engine` (additive):
- `contact_lists`: id, org_id, name, source_filename, status
  (`importing|ready|failed`), total_rows, accepted_count, invalid_count,
  duplicate_count, dnc_count, error (nullable), created_by, timestamps.
- `contact_list_rows`: id, org_id, list_id (FK, cascade), row_number, raw
  (PortableJSON), e164 (nullable), contact_id (nullable FK SET NULL), status
  (`accepted|invalid|duplicate|dnc`), reason (nullable), line_type (nullable).
  Index (list_id, status).
- `outbound_campaigns`: id, org_id, name, channel (`sms|voice`), list_id (FK),
  status (`draft|scheduled|running|paused|completed|cancelled`), body (nullable),
  template_id (nullable FK), from_numbers (PortableJSON list; empty = sticky/full
  pool), rate_per_minute (default 6), daily_cap (default 200), respect_warmup
  (default true), start_at (nullable), dialer_mode (nullable), parallel_lines
  (default 1), max_attempts (default 2), retry_backoff_minutes (default 240),
  local_presence (default false), created_by, timestamps.
- `outbound_sends`: id, org_id, campaign_id (FK cascade), row_id (FK), contact_id
  (nullable), e164, status (`queued|sending|sent|deferred|blocked|failed|skipped`),
  message_id (nullable FK), attempts (default 0), next_attempt_at (nullable),
  last_error (nullable), timestamps. UNIQUE (campaign_id, e164);
  index (campaign_id, status).
- `dial_attempts`: id, org_id, campaign_id (FK cascade), row_id (FK), contact_id
  (nullable), e164, status
  (`queued|dialing|connected|no_answer|busy|voicemail|failed|abandoned|completed`),
  call_id (nullable FK), amd_verdict (nullable), disposition (nullable),
  agent_user_id (nullable), attempts (default 0), next_attempt_at (nullable),
  timestamps. UNIQUE (campaign_id, e164); index (campaign_id, status).
- `org_numbers.warmup_started_at` (nullable datetime, no default).

Plus the Tier-1 `BULK_SEND_KEY` edit in `services/messaging.py` (DR-3).

## Allowed files (implementer may read anything; WRITE only these)

Backend implementer:
- `backend/app/services/pacing.py` (exists — Flash draft, Fable-reviewed; extend only
  if a ruling demands it)
- `backend/app/services/list_parsing.py` (exists — pure parse core, same provenance)
- `backend/app/services/list_import.py` (new — DB-aware import service on top of
  list_parsing)
- `backend/app/services/outbound.py` (new — campaign tick, claiming, sending)
- `backend/app/services/dialer.py` (new — dial tick, modes, retry, seam per DR-13)
- `backend/app/services/sweeper.py` (ONLY: add the two tick calls to `run_once`)
- `backend/app/api/routes/outbound.py` (new) + router include in `backend/app/main.py`
  (one line)
- `backend/pyproject.toml` (ONLY: add `openpyxl` — pre-approved by DR-8)
- `backend/tests/test_list_import.py`, `backend/tests/test_outbound_campaigns.py`,
  `backend/tests/test_dialer.py` (new)

Frontend implementer:
- `frontend/src/pages/ListsPage.tsx`, `frontend/src/pages/CampaignsPage.tsx` (new)
- nav/router wiring file(s), `frontend/src/api/hooks.ts`,
  `frontend/src/api/types.gen.ts` (regenerated)
- matching tests under `frontend/src/pages/__tests__/`

## Forbidden (all implementers)

- `backend/app/models/**`, `backend/migrations/**` — schema is done, hands off
- `backend/app/services/messaging.py`, `backend/app/services/sender.py` — Fable-owned
- `backend/app/compliance/**` — call it, never edit it
- `backend/app/providers/**`, `backend/app/routing/**`, `backend/app/voice_plane/**`
- `agents/**`, `.env*`, `deploy/**`, CI config
- the frozen seam tests (`test_compliance_seam.py` and P1/P2 spies)

## Test spec

Unit (backend):
- [ ] pacing: ramp caps by number age; effective cap = min(campaign, ramp); jitter
      bounded ±20%; predictive coefficient never lets projected abandon exceed 3%
- [ ] import: CSV and XLSX parse; mapping applied; invalid/duplicate/dnc rows get the
      right status + reason; accepted rows upsert contacts; counts on the list match
- [ ] import: opted-out contact (consent ledger) → row `dnc`
- [ ] claim: `sending` row with no message_id older than 5 min is re-queued; UNIQUE
      (campaign_id, e164) makes double-enqueue impossible

Integration (backend):
- [ ] **THE GATE:** a 500-row list (mixed: ~440 valid, invalids, dups, DNC) imports
      with a correct per-row report; an SMS campaign over it runs to completion via
      repeated `outbound_tick` calls on the loopback carrier: DNC rows skipped,
      quiet-hours sends deferred (frozen clock), daily cap honored per number, every
      row reaches a terminal status, per-row outcomes queryable via the API
- [ ] campaign send does NOT flip an `active` AI thread to `handed_off` (BULK_SEND_KEY)
- [ ] carrier failure → retry with backoff → `failed` after max_attempts
- [ ] pause/cancel: no further sends after status change mid-run
- [ ] dialer (fake seam): power mode dials queue in order; parallel mode N legs and
      first `connected` cancels siblings; `voicemail` AMD verdict → disposition
      voicemail + retry NOT scheduled; no_answer → retry scheduled; quiet-hours
      contact → `next_attempt_at` deferred, no dial
- [ ] predictive pacing slows after abandons (coefficient drops)

Frontend:
- [ ] ListsPage: upload → mapping preview → commit → row outcomes render
- [ ] CampaignsPage: create/start/pause; progress counts render

Manual (live, BLOCKED on B1/B2 — record in PROGRESS as runtime-blocked):
- [ ] live SMS campaign to real handsets (needs B1 Telnyx)
- [ ] dialer connects an agent to a live human; voicemail correctly classified (needs
      B2 trunk + I1 nginx wss)

Pass criteria: full backend suite green (SQLite) + ruff clean + frontend suite green +
OpenAPI drift gate green. CI (incl. Postgres) green after push.

## Deploy

yes — migrate + restart api on the VPS after review; live gates recorded as blocked.
