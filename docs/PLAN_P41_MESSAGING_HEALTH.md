# P41 — Messaging health: receipts on every carrier, per-workspace health, early warnings

Written 2026-09-16 from the operator's three asks: "for all providers we will have receipts,
like failed, delivered"; "metric measurement for all organisations - delivery, stop and all
other categories"; "so we can warn them before making any changes to their account".

## What already exists (verified in code, not from memory)
- **Receipts.** Every carrier parser maps delivered / failed / undelivered into the same
  message statuses (`providers/*/webhooks.py`; `models/messaging.py:32-36`), and
  `services/messaging.py:_ingest_dlr` applies them and writes `error_code`/`error_detail`.
  Plivo and SignalWire ask for receipts per message; Bandwidth, Telnyx and Twilio get them
  from a webhook URL configured in each carrier's dashboard/application.
- **STOP / HELP / START.** Fully built and tested: `compliance/keywords.py` (stop, stopall,
  unsubscribe, cancel, end, quit; start, unstop; help, info), `compliance/gate.py:188` →
  `compliance/service.py:handle_inbound_keyword` → `record_consent` (ledger row FIRST, then
  auto-reply), `is_opted_out`, DNC table. Tests: `test_optout_engine.py`, `test_keywords.py`.
  An earlier audit in this session claimed STOP was not detected; that was wrong.
- **Per-number reputation.** `services/reputation.py`: trailing 7 days per number -
  volume, delivery rate, carrier error rate, spam-class blocks (Bandwidth codes only);
  alert when delivery < 85% over ≥ 50 terminal sends or any spam block; ONE audit-log row
  per (org, number, day); runs from the sweeper (`services/sweeper.py:416`). API:
  `GET /api/v1/numbers/reputation`.
- **Analytics overview.** `services/analytics.py:overview` gives the dashboard a per-day
  messages series with `delivery_rate`, calls, campaigns, AI, spend. No opt-outs, no
  failures by cause, no thresholds.
- **In-app notifications** exist per user (`services/notifications.py:create`, bell +
  websocket). No email. Reputation alerts go to the audit log only - nobody is told.

## The gaps, in the operator's words
1. "Receipts for all providers" → receipts are built; what is NOT verified is that the
   three dashboard-side webhook URLs (Bandwidth application, Telnyx messaging profile,
   Twilio number) point at this deployment. **A check, not a build.** Also: Bandwidth
   spam-class error codes are the only ones classified; Telnyx/SignalWire/Twilio failure
   codes are stored raw and never bucketed, so "blocked as spam" is invisible on three of
   five carriers.
2. "Metrics for all organisations" → everything is per number or per day-series; there is
   no per-workspace health figure, no opt-out rate, no failure-by-cause split, no history
   table (every read recomputes over `messages`).
3. "Warn them before making changes" → the only alert is a per-number audit-log row that no
   human sees. There is no workspace-level threshold, no notification to the workspace's
   admins, no platform-ops view across workspaces.

## Goal
Every workspace has a daily messaging health record; its admins are warned in-app when it
crosses a threshold; platform ops sees all workspaces on one screen. Receipts are confirmed
on all five carriers and failure causes are bucketed uniformly.

## Design (Tier 1 decisions)

### 1. Failure-cause buckets, one vocabulary for all carriers
`providers/failure_classes.py` (new): map each carrier's raw `error_code` to one of
`spam_blocked | carrier_rejected | invalid_destination | opted_out | unknown`. Bandwidth's
existing 4750-4779 spam mapping moves here unchanged; Telnyx (e.g. 40001-40003 blocked,
40300s invalid destination), SignalWire/Twilio (30007 filtered, 30003/30005 unreachable,
21610 opted out) are added from the carriers' published code tables, each cited in a
comment. `_ingest_dlr` stores the bucket in a new `Message.failure_class` column.
Unknown codes stay `unknown` and are counted, never guessed.

### 2. Daily rollup table `org_messaging_daily` (migration 0044)
One row per (org, period_date, carrier), same shape discipline as `provider_spend_daily`:
`sent`, `delivered`, `failed`, `failed_spam_blocked`, `failed_carrier_rejected`,
`failed_invalid_destination`, `failed_opted_out`, `inbound`, `opt_outs` (ConsentEvent
opt_out rows that day), `help_requests`, `opt_ins`. Filled by a sweeper tick that upserts
"today" and "yesterday" every run (receipts arrive late) - idempotent by unique key.
Rates are derived at read time, never stored, so a late receipt cannot leave a stale rate.

### 3. Workspace health score and thresholds
`services/messaging_health.py`: for a workspace, trailing 7 days from the rollup:
`delivery_rate`, `failure_rate`, `spam_block_rate`, `opt_out_rate` (opt_outs / delivered),
`volume`. Thresholds are platform-level settings with defaults:
delivery < 90% (warn) / < 80% (critical), spam_block_rate > 1% (warn) / > 3% (critical),
opt_out_rate > 3% (warn) / > 5% (critical), each only when volume ≥ 100 in the window.
These numbers are the operator's to tune; the defaults are stated in the ops UI, not buried.
A per-number breach (existing reputation alert) also raises the workspace to "warn".

### 4. Warnings that reach a person
- **Workspace admins** get ONE in-app notification per (org, metric, level, UTC day) via
  `notifications.create` to every member with role `owner` or `admin`
  (`models/rbac.py:68`): "Delivery rate 84% over the last 7 days on 640 texts. Below 90%
  carriers start filtering your traffic." Plain words, the number, what happens next.
- **Platform ops** gets a list view: every workspace, its level, the three rates, volume,
  first-breached-at, sorted worst first. Route under `routes/platform.py`, ops-token gated.
- **No automatic action on the account** in this phase. The ask is to warn BEFORE changing
  anything; throttling or pausing a workspace is a later, explicit slice.

### 5. Carrier receipt verification (ops, one-time, in RUNBOOK)
For Bandwidth, Telnyx and Twilio: where the delivery-status webhook URL lives in each
dashboard and what it must equal. A `GET /api/v1/platform/messaging/receipts-check`
endpoint reports, per carrier, the timestamp of the last receipt actually ingested, so
"receipts are configured" is proven by data, not by a screenshot.

### 6. Surfaces
- Workspace: a "Messaging health" card on the existing Dashboard (`DashboardPage.tsx`,
  next to the spend tile): level badge, the three rates, 7-day volume, and the last warning
  text. No new page. Clicking opens the existing Numbers page reputation column.
- Platform ops: one table page. No charts.

## Allowed files (implementer)
Backend: `app/providers/failure_classes.py` (new), `app/services/messaging.py`
(`_ingest_dlr` only: set `failure_class`), `app/models/messaging.py` (one column),
`app/models/messaging_health.py` (new rollup model), `migrations/versions/0044_*.py`
(new), `app/services/messaging_health.py` (new), `app/services/sweeper.py` (register one
tick), `app/api/routes/analytics.py` (one `GET /health` under the org), `app/api/routes/platform.py`
(two GETs), `tests/test_p41_messaging_health.py` (new), `tests/test_failure_classes.py` (new).
Frontend: `src/api/hooks.ts` (two hooks), `src/pages/DashboardPage.tsx` (one card),
`src/pages/PlatformOpsPage.tsx` or the existing ops page (one table), their tests.
Docs: `RUNBOOK.md` (receipt URLs), `OPEN_ISSUES.md`, `ROADMAP.md`.

## Forbidden
`app/compliance/**` (STOP handling is correct; do not touch), `services/credits.py`,
`services/telephony_billing.py`, `services/plans.py`, `.env`, `deploy/**`, any carrier
adapter's send path.

## Test spec
Unit:
- [ ] `failure_classes`: every listed code maps to its bucket; an unlisted code → `unknown`;
      Bandwidth 4750-4779 still → `spam_blocked` (regression for reputation.py).
- [ ] `_ingest_dlr` sets `failure_class` on failed, leaves it NULL on delivered.
- [ ] Rollup tick: two runs the same day produce one row per (org, date, carrier), counts
      equal a hand-computed fixture; a late receipt re-run corrects yesterday's row.
- [ ] Health: rates computed from fixture rows; thresholds produce warn/critical/ok;
      volume below 100 never breaches; a per-number reputation breach raises to warn.
- [ ] Notifications: exactly one per (org, metric, level, day) even when the tick runs
      hourly; goes to owner + admin members only; text contains the rate and the volume.
- [ ] Platform list: sorted worst first; ops-token gated; workspace with no traffic shows
      "no data", not 0%.
- [ ] `receipts-check`: reports last receipt per carrier from real DLR rows.
Integration:
- [ ] Existing suites green: `test_optout_engine.py`, `test_keywords.py`,
      `test_p14_*`/reputation tests, `test_prepaid_telephony.py`, analytics tests.
Manual:
- [ ] Operator sets the three dashboard-side receipt URLs and `receipts-check` shows a
      recent receipt for each carrier after one test send each.
- [ ] Dashboard card renders for the Sabine workspace; ops table lists it.

Pass criteria: all unit + integration green before commit; manual items gate "live".

## Deploy
yes (migration 0044 additive; card and table are inert without rollup rows).

## Out of scope, named so nobody assumes it
Automatic throttling/pausing of a workspace; email delivery of warnings (no email stack
exists); carrier-side 10DLC campaign health APIs; per-number sending caps.
