# Launch Gaps — Current Source of Truth

**Status:** this file is the current source of truth for launch readiness. The following
earlier documents are **historical** and superseded by this one: `docs/HANDOFF.md`,
`docs/LAUNCH_READINESS.md`, and `docs/TELNYX_IMPLEMENTATION_GAPS.md`. Use them for background
only, never for planning, and update each to point here.

**Scope:** verified shipped state plus remaining launch blockers. No credentials, account
identifiers, phone numbers, host addresses, customer data, or secret values appear here, and
none may be added.

## 1. Launch verdict

Core telephony, messaging, and identity work is built, deployed, and green in production,
including open self-serve registration for both business and individual accounts and a
separate platform-admin login. **Launch is not yet approved.** Stripe is unconfigured, the
intended telephony credit gate is inactive, and several post-deploy external verifications
and ownership decisions remain open. Section 3 lists blockers in execution order: 1-2 are
configuration and rollout gates, 3-6 are verification gates, 7-8 are ownership and policy
gates.

## 2. Built and deployed ledger (verified)

**Repository state**
- GitHub `main`: `1e07e84`
- Implementation merge: `a7745d2`
- Identity branch `p41-kyc`: `8cbf4da`

**Production deployment**
- Deployment succeeded after the implementation merge.
- Public `/healthz` and `/status` are green.
- API, database, Redis, media plane, Bandwidth, Telnyx, and SignalWire all report up.
- Production Alembic current is the single head `0060_admin_invites`.
- A pre-migration database backup was taken at
  `/opt/csaas/backups/pre-20260921-public-admin.dump` before the public-registration and
  platform-admin migrations were applied.

**Telnyx carrier and messaging**
- Carrier routing, the real messaging adapter, and webhooks are built.
- 10DLC brand/campaign filing is built, including carrier-confirmed approval and number
  association.
- Toll-free verification (TFV) filing and reconciliation are built.
- Bounded approval evidence and send gating are built.
- The live Telnyx messaging profile was read successfully; its v2 webhook points at this
  app's Telnyx messaging webhook.

**Identity and onboarding**
- Individual signup/account model is built and deployed.
- Didit and manual verification flow, admin approval, voice-only policy, expired-identity
  handling, and pending polling are built and deployed.
- Didit runtime key, webhook secret, and workflow ID are present; the workflow is published
  and the destination is configured.
- No live end-to-end Didit identity session plus signed webhook has been run since deploy
  (blocker 3).

**Public registration and platform-admin enrollment**
- Public registration is enabled in production (`ALLOW_OPEN_REGISTRATION=true`).
- Self-serve signup supports both business and individual accounts.
- Migration `0059_org_kyc_required` grandfathers existing organizations with
  `kyc_required=false` and defaults future organizations to `kyc_required=true`.
- Per-organization telephony access enforces verification for new organizations, while the
  global `KYC_ENFORCED` flag remains false for legacy compatibility.
- A separate platform-admin login and signup are deployed with invitation-only admin
  enrollment.
- Migration `0060_admin_invites` backs the admin invitation enrollment flow.

**Billing correctness**
- Subscription cancellation now removes stale plan allowance safely.
- Caller-supplied Stripe customer identity is ignored.
- Both changes have focused passing tests.

**Deployment and configuration integrity**
- Deployment now quotes remote heredocs and renders LiveKit/SIP Redis config atomically.
- Production config was repaired and active Redis password fields are nonblank.
- Production JWT and session secrets pass a non-disclosing 32-character minimum check.

## 3. Blocking external and configuration work (execution order)

1. **Stripe secret key and webhook secret are missing.** Top-ups cannot be launch-ready
   until both are configured and a signed webhook and top-up smoke test passes.
   Exit criteria: both secrets present in the production secret store; a Stripe-signed
   webhook delivered and accepted; a top-up completed and reflected; result recorded.

2. **Telephony credit gate is inactive** (`TELEPHONY_PREPAID_DEFAULT=false`), so new
   workspaces are not behind the intended credit gate. Do not enable until Stripe/top-up
   works (blocker 1) and existing-organization impact has been checked.

3. **No live Didit round-trip after deploy.** Run one live identity session and verify the
   signed webhook end to end against the production deployment.

4. **No authorized Telnyx filing smoke.** An explicitly authorized billable Telnyx
   brand/campaign filing smoke has not been run. It must be explicitly authorized before it
   starts and must never be initiated from this documentation.

5. **Bandwidth delivery receipts.** Verify the delivery-receipt callback in the Bandwidth
   dashboard, send one controlled message per live carrier, then prove the recent receipts
   in the operator receipt-check surface. The Telnyx callback itself is already verified.

6. **Concurrency evidence gap.** Run the existing Telnyx concurrency harness against a
   disposable PostgreSQL instance — never against production — to close the SQLite row-lock
   evidence gap.

7. **TFV reconciliation ownership.** Assign an accountable operator, a durable audit-trail
   location, and a Telnyx support escalation owner for TFV reconciliation.

8. **Outstanding policy decisions.** Decide non-Telnyx unknown-registration enforcement,
   approval-refresh cadence, BYON policy, filing-fee policy, and the deploy rollback owner.

## 4. Nonblocking hardening

- Re-run the non-disclosing secret-length check after every secret rotation and treat a
  regression as a deploy failure.
- Keep heredoc quoting and atomic LiveKit/SIP Redis rendering; re-verify rendered config
  after every deploy-affecting change.
- Once blocker 7 assigns an owner, turn TFV reconciliation into a scheduled, auditable
  report rather than an ad hoc check.
- Once blocker 5 passes, use the receipt-check surface as the routine check after any
  carrier or sender change.

## 5. Evidence and verification commands

All commands below report status only and must not print secret, key, token, or
connection-string values.

**Health and deployment**
- `curl -fsS "$APP_BASE_URL/healthz"` — expect green.
- `curl -fsS "$APP_BASE_URL/status"` — expect API, database, Redis, media plane, Bandwidth,
  Telnyx, and SignalWire reported up.
- `alembic current` (run in the deployed service; do not echo the database URL) — expect the
  single head `0060_admin_invites`.

**Configuration integrity**
- Run the deployment's non-disclosing config check: `JWT_SECRET` and `SESSION_SECRET` at
  least 32 characters, and active Redis password fields nonblank. It must report pass/fail
  only; never add an option that prints values:

```
python - <<'PY'
import os
# Report presence/length only; never print values.
print({k: len(os.environ.get(k, "")) >= 32 for k in ("JWT_SECRET", "SESSION_SECRET")})
PY
```

**Billing correctness**
- `pytest backend/tests/test_subscriptions.py backend/tests/test_p24_billing_api.py`
  — both must pass.

**Telnyx carrier checks**
- Read the messaging profile without disclosing its URL, confirming the v2 webhook and that
  it targets this app's webhook:
  `curl -fsS -H "Authorization: Bearer $TELNYX_API_KEY" "$TELNYX_API_BASE/messaging_profiles/$MESSAGING_PROFILE_ID" | jq '{webhook_api_version, webhook_matches: (.webhook_url == env.EXPECTED_WEBHOOK_URL)}'`
- Concurrency harness on a disposable database only:
  `DATABASE_URL="$DISPOSABLE_PG_URL" pytest backend/tests/test_telnyx_postgres_concurrency.py`
  Requirement: `$DISPOSABLE_PG_URL` points at disposable PostgreSQL, never production; tear
  the instance down afterwards.

**Post-deploy external verifications (blockers 3-5)**
- Didit: run one live session and confirm the signed webhook lands; inspect the app's
  webhook/status surface rather than printing payloads or secrets.
- Bandwidth: dashboard callback check, then one controlled message per live carrier, then
  confirm the recent receipts in the operator receipt-check surface.
- Stripe (after blocker 1): deliver a signed webhook and complete a top-up smoke, then verify
  the recorded effect in the database without printing keys.

## 6. Documentation status

- This file supersedes `docs/HANDOFF.md`, `docs/LAUNCH_READINESS.md`, and
  `docs/TELNYX_IMPLEMENTATION_GAPS.md`; treat those as historical background.
- When a blocker closes, update the ledger in section 2 and the blocker entry here, and
  record the evidence path; do not start a parallel launch document.
- Link here from any document that previously carried launch-gap assumptions.
