# Leftover tasks — combined handoff (2026-09-27)

Merged from three sessions: 04 (transcripts/profile/help), 2b (monitoring/billing), Ringlite/c002
(discounts/10DLC/invoices). Repo `csaas`, box 144.126.152.175 `/opt/csaas`, compose
`deploy/docker-compose.prod.yml`, deploy = `bash deploy/deploy.sh` from a clean worktree
(its 30s health check can falsely say ABORT; the API needs ~40s — check `/healthz` by hand).
Live head after this deploy: **650ab15** (includes 0608257 auto-pause). Alembic head 0091.

Legend: **[USER]** needs a user decision/action first · **[CODE]** ready to build · **[OPS]** box work.

---

## A. Needs the user (do these first — nothing to code until answered)

1. **[USER] Transcription price $0.0025/min** — Switchboard → Console → Prices → `transcription_min`.
   Claude's DB write was denied by the classifier; do not retry via SQL. Until set, minutes are
   charged $0 and a `price_unset` alert accumulates the minutes.
2. **[USER] Recording price** — `recording_min` unanswered. Suggested $0.005/min (tier-3/4 peer average).
3. **[USER] Rotate the Groq key** — it was pasted in chat. New key → `/opt/csaas/.env` `GROQ_API_KEY=`
   (backup `.env.bak-groq-20260927` exists), then `docker compose ... up -d api worker`.
4. **[USER] Support contact details** — email, phone, KB links for the Help menu / Support tab
   (`frontend/src/components/HelpMenu.tsx`, `SupportTab.tsx`, backend `services/support.py`).
5. **[USER] Live test call** — recording notice timing, hold, transfer/add (internal only), leave,
   park/pickup. Do it with the user on the phone.
6. **[USER] Custom invoices live Stripe smoke test** (Ringlite) — shipped ec4a173, tested only
   against a fake Stripe. Risk: API 2026-08-26.dahlia may reject `InvoiceItem.create(amount=..., invoice=...)`
   or `Invoice.create(pending_invoice_items_behavior="exclude")` → row shows "failed" in Ops (no charge
   happens before finalize). With the user's OK: $1 "item" invoice to the user's own workspace (card
   on file), then check Ops → Console → workspace → Invoices, Stripe, and `billing_payments`
   (kind='invoice', state='paid'). Files: `backend/app/services/custom_invoices.py`, `tests/test_custom_invoices.py`.
7. **[USER] Stripe webhook events** — add `invoice.payment_failed` and `invoice.voided` to
   `https://ringlite.io/api/v1/webhooks/stripe` in the Stripe dashboard (code already handles them).
8. **[USER] Refund claw-back decision** — refunding a custom invoice does not take back granted
   packages/credit (reference `invoice:<id>:<line>`). Needs a `charge.refunded` handler if wanted.
9. **[USER] Invoiced packages roll-over?** — today they expire at monthly renewal like bought bundles
   (entry_type "purchase"). Roll-over = new entry_type in `custom_invoices.apply_paid` + 2b's `expire_unused` rule.
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

1. **[CODE] 2 pre-existing failures** `tests/test_voice_plane.py::transfer_room_call_*` — tests still
   expect external transfer; the rule is internal-only transfers. Update the tests (not the rule).
2. **[CODE] Remove dead Deepgram helpers** in `backend/app/services/monitor_calls.py`
   (`DEEPGRAM_URL`, `_utterances_to_segments`, `_meter_deepgram`) and their test
   `tests/test_lkrec_wiring.py::test_monitor_deepgram_request_is_metered_as_platform_cost`.
   Keep `deepgram_api_key` in config (the live call-monitor worker still uses nova-3 as lkrec fallback).
3. **[CODE] Full backend suite runtime** — locally it runs >30 min / appeared to hang once; find the
   slow/hanging test (`pytest --durations=25`), consider pytest-timeout (ask before adding the dependency).

## D. Box cleanup

1. **[OPS] Delete the Parakeet model dir** `/opt/csaas/var/stt-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8`
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
