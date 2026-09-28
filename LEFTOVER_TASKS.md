# Leftover tasks — combined handoff (2026-09-27)

Merged from three sessions: 04 (transcripts/profile/help), 2b (monitoring/billing), Ringlite/c002
(discounts/10DLC/invoices). Repo `csaas`, box 144.126.152.175 `/opt/csaas`, compose
`deploy/docker-compose.prod.yml`, deploy = `bash deploy/deploy.sh` from a clean worktree
(its 30s health check can falsely say ABORT; the API needs ~40s — check `/healthz` by hand).
Live head after this deploy: **650ab15** (includes 0608257 auto-pause). Alembic head 0091.
**Update 2026-09-27 (Ringlite):** box verified = origin/main 5b86129 (file hashes). main is now
**6839fa0** (invoice pay-link, no migration) — DEPLOYED 2026-09-27 (live head 5563bad, healthz 200).
~~Uncommitted edits in `csaas_ship` / `csaas_site`~~ DISCARDED 2026-09-28 (user): both content-equal
to main (a6dc07c, 85cc38d); both worktrees reset to origin/main, branches deleted.
**Update 2026-09-28 (session 19):** branch `feat/summary-billing-ext-transfer` (worktree `csaas_site`,
on top of Ringlite's undeployed 6d1fb0a) — NOT pushed/deployed, waiting for the user's go:
4faa3eb AI summaries paid (`summary_min`, feature `call_summary`), af95ace external transfers
(feature `external_transfer`, bridged outbound call), eba3258 dead Deepgram helpers removed.

**Update 2026-09-28 (Ringlite, combined deploy with 19):** branch `feat/min5-autorecharge-signup`
rebased on bdc4c8d: $5 minimum on custom invoices; auto-recharge $5 steps; Telnyx balance alert
< $25 (sweeper, `services/telnyx_balance_alert.py`, emails ops admins once per drop); legal pages
/legal/{privacy,terms,refunds} (+ /privacy etc.), footer "Powered by Sabine Property Group LLC";
Checkout top-ups save the card (setup_future_usage) and the FIRST paid top-up turns auto-recharge
on (below $5 -> $10, never re-enabled later); signup onboarding step `funding` -> /add-credit
(min $5) between approval and numbers. No migration.
**After deploy [USER/Claude in Chrome]:** Stripe public name (DBA) Ringlite, website ringlite.io,
privacy/terms URLs, support email support@ringlite.io, invoice prefix RL, branding logo;
statement descriptor stays SABINE-based (AdAgentIQ shares the account). Stripe also shows
"Business ownership information: Incomplete" and account representative "Invalid" — user must fix.

Legend: **[USER]** needs a user decision/action first · **[CODE]** ready to build · **[OPS]** box work.

---

## A. Needs the user (do these first — nothing to code until answered)

   **DONE 2026-09-28 17:00 UTC (Claude, owner's request):** platform_prices rows written via the
   console route's logic (note on each row): recording_min 2500, transcription_min 20000,
   summary_min 3500 micros; open price_unset alerts for recording/transcription marked reviewed.
   Verified through telephony_billing.platform_price. Owner said to LEAVE the per-workspace
   switch-on of call_summary / external_transfer - both still off everywhere.
1. **[USER] Prices DECIDED 2026-09-28** (industry −20%, user-set): `recording_min` **$0.0025**,
   `transcription_min` **$0.02**, `summary_min` **$0.0035** (per call minute). Set them in
   Switchboard → Console → Prices after the deploy (summary_min exists only after 4faa3eb ships),
   then switch on `call_summary` / `external_transfer` per workspace. Our cost: recording ≈ $0 (own
   box), Groq ≈ $0.0013/call-min (both sides), DeepSeek summary ≈ $0.0001/min.
   (old text) Transcription price — Switchboard → Console → Prices → `transcription_min`.
   Claude's DB write was denied by the classifier; do not retry via SQL. Until set, minutes are
   charged $0 and a `price_unset` alert accumulates the minutes.
2. ~~Recording price~~ — decided above.
3. **[USER] Rotate the Groq key** — it was pasted in chat. New key → `/opt/csaas/.env` `GROQ_API_KEY=`
   (backup `.env.bak-groq-20260927` exists), then `docker compose ... up -d api worker`.
4. **[USER] Support contact details** — email DECIDED: support@ringlite.io (legal pages use it;
   phone +1 469 461 7576 from Stripe). Still open: KB links, and wiring the email/phone into for the Help menu / Support tab
   (`frontend/src/components/HelpMenu.tsx`, `SupportTab.tsx`, backend `services/support.py`).
5. **[USER] Live test call** — recording notice timing, hold, transfer/add, leave, park/pickup,
   and (after af95ace ships) an EXTERNAL transfer: outside party answers → agent drops, both
   calls bill, either hang-up ends the room. Unverified: does the recording capture the outside
   party? Do it with the user on the phone.
6. **[USER] Custom invoices live Stripe smoke test** (Ringlite) — 2026-09-28: pay-link half DONE
   and PASSED ($1.50, Sabine Property Group, TESZJMFV-0001, paid via webhook). Charge-card half
   BLOCKED: Sabine has no card on file — user adds one, then run the $1 item test.
   Original note: — shipped ec4a173, tested only
   against a fake Stripe. Risk: API 2026-08-26.dahlia may reject `InvoiceItem.create(amount=..., invoice=...)`
   or `Invoice.create(pending_invoice_items_behavior="exclude")` → row shows "failed" in Ops (no charge
   happens before finalize). With the user's OK: $1 "item" invoice to the user's own workspace (card
   on file), then check Ops → Console → workspace → Invoices, Stripe, and `billing_payments`
   (kind='invoice', state='paid'). Files: `backend/app/services/custom_invoices.py`, `tests/test_custom_invoices.py`.
   Since 6839fa0 the form also has "Email a pay link" (Stripe `send_invoice` to a workspace OWNER
   only, due in N days; granted on the `invoice.paid` webhook).
   **Pay link PASSED live 2026-09-27**: $1.50 item, Sabine Property Group, billing_payments
   2ea056ce-f438-42f5-8651-c35267670185 / Stripe TESZJMFV-0001, paid, webhook 204, row paid.
   **Charge-card still untested**: that workspace has no card on file — user adds one first.
   Since 6d1fb0a (DEPLOYED 2026-09-28) package lines take exact `units` (e.g. 760 call minutes)
   and/or a price, optional `rate_micros`; default rate = pay-as-you-go (sms_out, mms_out,
   voice_min_out), not the bundle price.
6b. **[USER] Email code latency** — measured: app → Telnyx 1-3 s, Telnyx → Gmail delivered +18-66 s
   (Zoho +372 s). Not our code. SPF `~al` typo fixed by the user. Fix = transactional provider
   (Postmark or Resend) as primary for codes: user creates the account, adds DNS for
   `mail.ringlite.io`, puts the key in `/opt/csaas/.env`; then [CODE] reorder `services/mailer.py`
   `send()` so codes try that provider first, Telnyx fallback, and re-measure.
   **DONE 2026-09-28 (Claude, via Stripe API):** we_1UIdIT744iNFjjqnsFf9UBaX now has 18 events incl.
   invoice.payment_failed (already there), invoice.voided and charge.refunded (added).
7. **[USER] Stripe webhook events** — add `invoice.payment_failed` and `invoice.voided` to
   `https://ringlite.io/api/v1/webhooks/stripe` in the Stripe dashboard (code already handles them).
8. ~~Refund claw-back decision~~ DECIDED + BUILT 2026-09-28 (dbb1d01, 70d0f74): refundable = ONLY unused paid credit, less Stripe fees (customer bears card fees both ways); bundles/spent credit/plans/number+10DLC fees never. Ops -> Billing -> "Refund unused credit" issues it; refunds made in Stripe take back the unused part (fee-grossed), excess -> `refund_shortfall` alert; P&L nets refunds, keeps fees. [USER] add `charge.refunded` to the Stripe webhook; policy page /legal/refunds = Ringlite branch.
   Original note: **Refund claw-back decision** — refunding a custom invoice does not take back granted
   packages/credit (reference `invoice:<id>:<line>`). Needs a `charge.refunded` handler if wanted.
9. ~~Invoiced packages roll-over?~~ DECIDED 2026-09-28 (user): NOTHING rolls over - plan allowances, invoiced packages and bought bundles all expire at the monthly renewal (already the code: plans per period, bundles.expire_unused, invoice packages = purchase entries). Stated on the bundle card; legal pages = Ringlite.
   Original: **Invoiced packages roll-over?** — today they expire at monthly renewal like bought bundles
   (entry_type "purchase"). Roll-over = new entry_type in `custom_invoices.apply_paid` + 2b's `expire_unused` rule.
   **DONE b5e623c, DEPLOYED 2026-09-28 17:47 UTC** (owner: "do others"): services/monitor_oversight.py
   (rescore hourly, shared_recipients signal, daily ops digest, weekly spot checks) + sweeper wiring,
   monitor_score.recompute(repeat_recommendation=False), config monitor_overlap_* / spot_check_count,
   tests/test_monitor_oversight.py (8, mutation-checked). Nothing pauses/restricts. DB backup
   backups/pre-oversight-2026-09-28-1733.sql.gz.
10. **[USER] Monitoring steps 2–5 file-list OK** (2b) — each needs its file list approved (>3-files
    rule); security logic → build directly, not delegated. User policy (verbatim): "major actions like
    pausing or deleting organisation or disableing it. or any feature should be only for human" —
    the hard-evidence auto-pause (0608257) is the ONLY approved exception.
    - 2: hourly recompute of every `org_monitoring` row (today only in `add_signal`); module-level
      monotonic gate in the sweeper like `_bundle_expiry_last_run` in `telephony_billing.py`.
    - 3: cross-tenant shared-recipient overlap signal (many workspaces hitting the same recipients) → new signal kind.
    - 4: daily digest email to ops admins (new pauses, recommendations, top scores) via
      `break_glass.admin_emails` + `mailer.send`.
    - 5: weekly random spot-check queue of workspaces for a thorough review.

## B. (checked 2026-09-28 16:30 UTC) B1 ok (no pauses, no monitor errors); B2 key present, no
groq errors, but NO transcription jobs in 3 days - Groq path still unexercised; B3 3/3 scored
calls have summaries; B4 no suspicious verdicts, stt without Parakeet; B5 67 passes, 0 expiries.

## B. Verify right after this deploy (650ab15)

1. **Auto-pause (2b):** `docker compose exec api grep -n "CONFIRMED_KINDS\|_email_admins_auto_paused\|admin_notify" /app/app/services/monitor_score.py`
   finds all three; no monitor_*/case_file exceptions over a few sweeper passes; read-only
   `SELECT org_id, level, paused_reason FROM org_monitoring WHERE level='paused'` shows no unexpected new pauses.
2. **Groq transcripts (04):** api env has GROQ_API_KEY (presence only, never print it); next queued
   `transcription_jobs` get `engine='groq-turbo'`, `ai_usage` rows provider `groq`; no `groq_stt` errors.
3. **Summaries:** a scored call's detail returns `ai_summary`/`ai_sentiment`; Summary card shows on the call page.
4. **Fraud review:** after the night window, reviews run on local Zipformer; any suspicious/scam
   verdict logs `call_review_verified` (Groq re-check). stt container starts without Parakeet.
5. Bundle expiry: first `bundle_expiries` count in telephony_tick output = 0.

## C. Ready to code (no user input needed)

1. ~~2 pre-existing failures `transfer_room_call_*`~~ DONE af95ace: external transfers now exist
   (bridged, opt-in, billed); the REFER tests use an own number.
2. ~~Remove dead Deepgram helpers~~ DONE eba3258. in `backend/app/services/monitor_calls.py`
   (`DEEPGRAM_URL`, `_utterances_to_segments`, `_meter_deepgram`) and their test
   `tests/test_lkrec_wiring.py::test_monitor_deepgram_request_is_metered_as_platform_cost`.
   Keep `deepgram_api_key` in config (the live call-monitor worker still uses nova-3 as lkrec fallback).
3. ~~Full backend suite runtime~~ RESOLVED (90edabd xdist + pytest-timeout): 2026-09-28 on this
   laptop `pytest -n 8 --timeout=300` = 3803 passed, 16 skipped, 0 failed in 44 min; no hang;
   slowest test 75 s (test_outbound_campaigns 500-row list). Windows needs `--basetemp`.
   Original: 3. **[CODE] Full backend suite runtime** — locally it runs >30 min / appeared to hang once; find the
   slow/hanging test (`pytest --durations=25`), consider pytest-timeout (ask before adding the dependency).

## D. Box cleanup

1. ~~Delete the Parakeet model dir~~ DONE 2026-09-28 (user OK). Zipformer punctuation verified live
   (sherpa punct model loads; both batch and live paths add punctuation + casing). `/opt/csaas/var/stt-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8`
   (user asked to remove Parakeet; code no longer loads it). Deletion — confirm with the user first.

## Reference
- Decisions 2026-09-27: customer transcripts = Groq whisper-large-v3-turbo (hallucination filter
  `no_speech_prob`/`avg_logprob`, 2 attempts then local Zipformer); summaries = DeepSeek V4 Flash via
  `services/scoring.py`; fraud review = local Zipformer after hours only, suspicious/scam re-verified
  with Groq (fail closed: if verification fails the first verdict stands). Parakeet removed.
- Worktrees: `csaas_prof` (feat/groq-transcripts, this), `csaas_oa` (2b, clean), `csaas_disc`
  (Ringlite, clean), `csaas_plan`.
- Memory: csaas_monitoring_v2_lkrec.md, csaas_custom_invoices.md, csaas_individual_10dlc_hard_gate.md,
  csaas_org_discounts.md, csaas_p46_pnl_entitlements.md.
