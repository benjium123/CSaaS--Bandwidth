# Launch Readiness — Telnyx 10DLC / TFV Registration

> **Status: NOT launch-ready.** Corrects `docs/TELNYX_IMPLEMENTATION_GAPS.md` (predates the
> filing and reconciliation code) and supersedes earlier drafts; the original is left
> unedited. Claims cite symbols in the audited files only. No secrets, no live carrier calls,
> and no live-carrier integration test yet — production Telnyx credentials remain an external
> prerequisite (§6). Integration evidence below is drawn from verified commits `ddbdd89`,
> `81df130`, `43e1ff9`, `907b829`, `1d95fbf`, `0719e59`, `9b9e261`, `ef724d8`, `87b63db`,
> `df849d3`, `c6284e0`.

## 0. Sources inspected

`api/routes/registration.py`, `api/routes/numbers.py`, `compliance/registration.py`,
`compliance/telnyx_approval.py`, `services/registration.py`,
`services/telnyx_brand_filing.py`, `services/telnyx_campaign_filing.py`,
`services/telnyx_tollfree_filing.py`, `services/telnyx_number_association.py`,
`providers/telnyx/registration.py`, `providers/telnyx/tollfree_verification.py`,
`providers/telnyx/tollfree_payload.py`, and the offline tests in §8. The TFV module
`services/telnyx_tollfree_filing.py` exposes `file_tollfree_verification_with_telnyx` and
`reconcile_tollfree_filing_with_telnyx`; the transport side exposes
`TelnyxTollfreeVerificationClient.list_requests`. The bounded-approval-evidence helper is the
pure module `compliance/telnyx_approval.py`. Referenced but not supplied, therefore
**UNVERIFIED**: `provider_accounts`, `providers/telnyx/registration_status`
(`map_brand_status`, `map_campaign_status`, `APPROVED`), `brand_payload`/`campaign_payload`,
`config.Settings`, `models.numbers`, `auth.deps`.

## 1. Corrections to the gaps doc and earlier drafts

- A1/A2 ("store the Telnyx ref", "idempotency") were **NOT BUILT**; **now built** — filing
  writes `carrier_refs["telnyx"]` on success and commits a durable attempt marker first.
- Approval was operator-asserted; it is **now fail-closed** — an `approved` decision is
  accepted only after a fresh Telnyx GET confirms the matching record approved.
- Stale approval is **now represented as separate bounded evidence, not a status change** — a
  carrier-confirmed approval records `carrier_refs["telnyx_approval"]` (state
  `approved`/`revoked`, UTC `checked_at`, exact carrier id, `source`), refreshed or revoked
  only through operator+compliance guarded, read-only POST `refresh-telnyx` routes whose only
  carrier interaction is a GET; the send gate consults that evidence (§2, §3).
- D2 (no attachment guard) is **built** for Telnyx: the PATCH path uses a carrier association
  service, the order path refuses a Telnyx `campaign_id` before spend, and the send gate is
  fail-closed.
- A `/status` no-op used to return a misleading 200; **now** `_apply_status_or_conflict`
  rolls back and raises `ConflictError`, never reporting an ignored decision as applied.
- Earlier drafts stated TFV had no filing route, no attempt marker, no way to write its
  `carrier_refs["telnyx"]`, and no reconciliation — **all now built** (see §2). The claim that
  `TfvOut` hides `carrier_refs` is **obsolete**: `TfvOut.carrier_refs` is now exposed.

## 2. Actually implemented (EXISTS)

**Carrier-confirmed approvals — `api/routes/registration.py`.** `set_brand_status`,
`set_campaign_status`, `set_tfv_status` accept a `StatusIn`; `_is_approval` routes an
`approved` decision through `_require_brand_approved`/`_require_campaign_approved`/
`_require_tfv_approved`, each of which demands a stored `carrier_refs["telnyx"]`, does a
fresh Telnyx GET, refuses on any transport/HTTP error, requires the returned id to equal the
ref, and requires the mapped status approved (`map_brand_status`/`map_campaign_status` ==
`APPROVED`; TFV `verificationStatus == "verified"`). Tests inject
`app.state.telnyx_http_client`. The **first** carrier-confirmed `approved` decision also
writes the bounded approval evidence below, atomically with the status advance.

**Bounded approval evidence — `compliance/telnyx_approval.py` (pure).** Approval is recorded
as bounded evidence as well as a terminal status. The written value is
`carrier_refs["telnyx_approval"]`, bound to the **exact** carrier id, with a state of
`approved` or `revoked`, a UTC `checked_at`, and a `source` of `status_decision` (written by
the first confirmed `/status`) or `refresh` (written by the refresh routes). Freshness is
bounded by `TELNYX_APPROVAL_MAX_AGE_SECONDS` (default `604800`; validated in the inclusive
range `300..2592000`; there is **no disable**). The module is pure — no I/O, no carrier call
(commit `87b63db`).

**Approval refresh / revoke — `api/routes/registration.py` (operator+compliance guarded;
local POST routes whose only carrier interaction is a read-only GET).** The three
`refresh-telnyx` endpoints for brand/campaign/TFV are HTTP POST routes; they make a fresh
carrier GET and then either refresh or revoke the stored approval evidence **without changing
the terminal local status**. A carrier error or a mismatched carrier id mutates nothing
(commit `df849d3`).

**Terminal status vs. separate evidence.** A terminal local `approved` is **intentionally not
demoted** (the state machine stays monotonic; D-3). A later carrier downgrade/suspension is
represented as **separate revoked or stale approval evidence**; it is the send gate — not the
registration status — that then blocks sending. This keeps the terminal status intact while
still refusing to send on a withdrawn approval.

**Legacy `/submit` routes are local-only.** The brand/campaign (`/submit`) and TFV (`/submit`)
paths only advance the local registration status; they do **not** call Telnyx and do **not**
write `carrier_refs["telnyx"]`. A local `submitted` state therefore does **not** imply a
carrier reference — only the `/file-telnyx` services below perform carrier filing.

**Status application — `_apply_status_or_conflict`.** `advance_status` is monotonic; when it
returns false (already current, stale after terminal, or terminal) the transaction is rolled
back and a non-PII `ConflictError` (`_NO_CHANGE_MESSAGE`) is raised — a no-op is never a 200.

**Brand filing — `services/telnyx_brand_filing.py::file_brand_with_telnyx`** (routed at
`POST /brands/{id}/file-telnyx`). Row-locks; refuses terminal/unknown status and anything
outside `{draft, submitted}`; validates fields; refuses a repeat when `carrier_refs` holds
`telnyx` or the `telnyx_brand_filing` marker; commits the pending marker before
`POST /10dlc/brand`; on 2xx writes `carrier_refs["telnyx"]`, clears the marker and advances
to `submitted`. On error the marker stays and status is untouched.

**Campaign filing — `services/telnyx_campaign_filing.py::file_campaign_with_telnyx`** (routed
at `POST /campaigns/{id}/file-telnyx`). Row-locks; admits `{draft, submitted}`; refuses an
existing `telnyx` ref or `telnyx_filing` marker; requires the brand locally approved AND
carrying a Telnyx ref; confirms the brand at Telnyx; commits an `unconfirmed` marker before
`POST /10dlc/campaignBuilder`; only a readable 2xx records `carrier_refs["telnyx"]` and
advances to `submitted`.

**TFV filing — `services/telnyx_tollfree_filing.py::file_tollfree_verification_with_telnyx`**
(routed at operator+compliance guarded `POST /tollfree/{tfv_id}/file-telnyx`). Takes a strict,
explicit `FileTfvTelnyxIn`; row-locks and refuses repeats when a `telnyx` ref or the TFV
attempt marker is present; validates fields; commits a durable pre-POST marker before issuing
the carrier POST; on 2xx stores the **exact returned id** as `carrier_refs["telnyx"]`. It
performs **no local approval** — status stays `submitted` and approval still flows through
`_require_tfv_approved` — and there is **no blind POST retry** (the marker blocks it).

**TFV reconciliation — `services/telnyx_tollfree_filing.py::reconcile_tollfree_filing_with_telnyx`
+ `TelnyxTollfreeVerificationClient.list_requests`** (routed at operator+compliance guarded
`POST /tollfree/{tfv_id}/reconcile-telnyx`). For an ambiguous-timeout filing (marker present,
no ref), the transport `list_requests` performs exactly **one GET** using the official phone /
business / date filters; the reconciler adopts a result **only when exactly one** request
matches (exact match). It **never retries the POST and never approves locally**; zero,
multiple, or mismatched results leave the marker in place and now have a written operator
process: `docs/runbooks/TELNYX_TFV_RECONCILIATION.md` (§3.2, §4.2).

**Number association — `services/telnyx_number_association.py::associate_number_with_telnyx`**
(reached from `numbers.py::assign_campaign` for Telnyx numbers). Locks number then campaign;
same-org; active Telnyx local only; campaign locally `approved` AND carrying a Telnyx ref;
confirms the campaign at Telnyx; commits a `provisioning["telnyx_campaign_assignment"]` marker
before `POST /10dlc/phone_number_campaigns`; sets `campaign_id` only after a 2xx whose echoed
phone number equals the requested E.164. Clearing a Telnyx `campaign_id` is refused (no
carrier unassign transport).

**Order-path guard — `api/routes/numbers.py::order`.** Before the credit gate and
`provider.order_number`, an order naming a Telnyx carrier with a `campaign_id` is rejected
(`ValidationFailedError`), so nothing is bought for an association that cannot be recorded
at order time; the operator orders first, then PATCHes `/{number_id}/campaign`.

**Send gate — `compliance/registration.py`.** For `carrier == "telnyx"`, `unknown` is a hard
refusal (`must_register_here`) and `approved` requires the campaign's `carrier_refs["telnyx"]`
plus an `assigned` `provisioning` marker matching that campaign and carrier id (local), or the
TFV's `carrier_refs["telnyx"]` (toll-free). Campaign/TFV sending additionally requires
**fresh, matching, `approved` `carrier_refs["telnyx_approval"]`** — a revoked or stale
record blocks sending (commit `df849d3`). The gate reads persisted rows only; there is **no
network call and no new carrier query** on the send path. Non-Telnyx carriers keep
allow-on-unknown unless `REQUIRE_NUMBER_REGISTRATION`.

**Transports / payload.** `TelnyxRegistrationClient`, `TelnyxTollfreeVerificationClient`, the
pure `build_tollfree_verification_payload`; single attempt, no auto-retry, no secrets logged.

**Operator runbook.** `docs/runbooks/TELNYX_TFV_RECONCILIATION.md` (commit `c6284e0`)
documents the guarded reconcile route, the zero/multiple/mismatched/timeout cases, the
malformed-marker and resolved-record cases, evidence handling, escalation, a controlled
marker-repair procedure, an incident/audit template, rollback steps, prohibitions, and a
verification checklist. The runbook is written; assigning the named operational owner and the
audit-trail storage remains an owner decision (§3.1, §6).

## 3. Remaining work (code / product / external)

1. **Product/ops — reconciliation runbook written; owner/audit-trail assignment open.** The
   operator runbook is now written: `docs/runbooks/TELNYX_TFV_RECONCILIATION.md` (commit
   `c6284e0`). It covers zero/multiple/mismatched timeout reconciliation, carrier
   timeout/error, listed/normal use of the guarded
   `POST /tollfree/{tfv_id}/reconcile-telnyx` path, a malformed or inconsistent durable
   marker, request-id/marker mismatches, and a controlled marker-repair procedure. The
   residual blocker is an owner decision: assigning the **named operational owner**, the
   **audit-trail storage** location, and the Telnyx support workflow (§4.1, §6). Hand-editing
   `carrier_refs`/markers as evidence must never be done casually, and the runbook states that
   prohibition explicitly.
2. **External — credentials and live verification.** Production Telnyx credentials, active
   provider account(s), and live sandbox/authorized verification are external prerequisites.
   No secrets belong in the repo or docs.
3. **Owner decisions.** Filing budget / current carrier fees, KYC scope, prepaid/settlement,
   the non-Telnyx `REQUIRE_NUMBER_REGISTRATION` default, BYON, pricing, deploy/rollback, and
   the approval-freshness window policy (the `TELNYX_APPROVAL_MAX_AGE_SECONDS` default/range
   and who operates the read-only POST `refresh-telnyx` refresh/revoke routes).

## 4. P0 launch blockers (ordered)

1. **Reconciliation runbook written; owner/support workflow open (product/ops).** The runbook
   now documents a safe operator process (`docs/runbooks/TELNYX_TFV_RECONCILIATION.md`), so
   zero/multiple/mismatched results from `reconcile_tollfree_filing_with_telnyx` — which
   retain the marker — have a written path. The residual blocker is the owner decision on the
   named operational owner, the audit-trail storage, and the Telnyx support workflow (§3.1,
   §6, §8).
2. **No real credentials / no live verification (external).** Production Telnyx credentials
   and an active provider account are external prerequisites, and no test exercises the real
   API (§3.2, §6, §8).

> The former P0 **stale-approval / revocation code gap is now implemented** — bounded approval
> evidence (`compliance/telnyx_approval.py`) plus operator+compliance guarded, read-only POST
> `refresh-telnyx` refresh/revoke routes (carrier GET only), consulted by the send gate (§2).
> It is no longer a blocker.

## 5. P1 (non-blocking hardening)

- Note that SQLite makes `with_for_update` a no-op (Postgres-only concurrency).
- Decide the `REQUIRE_NUMBER_REGISTRATION` default for non-Telnyx carriers.

## 6. Owner decisions, credentials and deploy (not code)

- Written authorization and budget for **billable, non-refundable** 10DLC filings; confirm the
  current fee/refund schedule and carrier number-association fees from the carrier's published
  terms (no amounts asserted here).
- Production Telnyx credentials / active provider account(s), provisioned and rotated outside
  the repo (never in docs or logs); filing and the guards cannot work without them.
- Whether the bounded-freshness window is acceptable: the `TELNYX_APPROVAL_MAX_AGE_SECONDS`
  default (`604800`) and validated range (`300..2592000`, no disable), and who is accountable
  for operating the read-only POST `refresh-telnyx` refresh/revoke routes (whose only carrier
  interaction is a GET). A named **owner** (and **audit-trail** storage) is still required for
  the written reconciliation runbook and the Telnyx support workflow (§3.1, §4.1).
- Per-environment `REQUIRE_NUMBER_REGISTRATION` default; KYC scope; prepaid/settlement policy;
  BYON decision; pricing pass-through.
- Deploy window and rollback owner; a migration only if `carrier_refs` is proven insufficient.

## 7. Security risks (beyond missing features)

- **Stale carrier evidence can still slip if the freshness policy is weak.** The send gate now
  consults bounded, exact-id approval evidence (`carrier_refs["telnyx_approval"]`) and blocks
  on revoked or stale records, so a withdrawn approval no longer keeps sending while the
  terminal status stays intact. The residual risk is policy, not code: the window
  (`TELNYX_APPROVAL_MAX_AGE_SECONDS`, default `604800`) and the cadence of the manual
  refresh/revoke routes determine how quickly a downgrade is noticed.
- **Reconciliation dead-ends can still pressure operators to forge evidence.** The runbook now
  documents the safe reconcile path and the repair guardrails
  (`docs/runbooks/TELNYX_TFV_RECONCILIATION.md`), but zero/multiple/mismatched results still
  leave the marker and force a controlled manual step; the residual temptation is to hand-edit
  `carrier_refs` — out-of-band evidence the guard then trusts — which the runbook prohibits.
  The two-person / backup / audit guardrails need an assigned owner to be effective.
- **Unknown-registration allowance for non-Telnyx carriers:** allow-on-unknown is the default;
  the only tightening is an env flag, so the control is opt-in there.
- **Mock-only confidence.** A shape change in `registration_status`/`verificationStatus` can
  mis-map and silently approve.

## 8. Verification: offline tests done, live carrier still missing

Offline tests (real service + `httpx.MockTransport`; **no live Telnyx call**) are done and were
run on the integration branch. Historical evidence: after the filing route landed, **319**
Telnyx / registration tests passed; after reconciliation landed, **224** tollfree/TFV-selected
tests passed and **52** route+reconciliation tests passed.

For the bounded-approval / revocation pass and the order-route guard, targeted runs reported
**103** evidence/config tests passed, **37** existing affected Telnyx registration tests
passed, and **29** new refresh/send-gate tests passed, with **Ruff and diff checks clean**.
The dedicated order-route guard test `backend/tests/test_telnyx_order_campaign_guard.py` was
added (commit `c6284e0`). A broader selector run reported **587 passed, 2402 deselected, and 1
failure**: `backend/tests/test_bugfix_area2.py::test_send_message_skips_registration_gate_when_plan_supplied`,
which fails in prepaid billing (a `TelephonyCreditsError`) **before** reaching the registration
gate. That failure is **unrelated to this change**, and the broad run is **not** claimed fully
green.
Coverage includes approval guards
(`test_telnyx_brand_approval_guard.py`, `test_telnyx_campaign_tfv_approval_guard.py`): missing
ref / carrier pending / mismatched id / wrong TFV field / confirmed approval / a repeated
approval now returning 409; approval-evidence freshness, refresh, and revoke tests; the
send-gate tests for stale/revoked evidence; the dedicated order-route guard; number
association (`..._success.py` + `..._failures.py`): success, pending, timeout, wrong-phone and
repeat markers; payload tests for field and enum validation; TFV filing and reconciliation
route tests.

**Still missing:** live-carrier verification (production credentials, §6) and an assigned owner
and audit-trail storage for the written reconciliation runbook (P0, §4.1).
