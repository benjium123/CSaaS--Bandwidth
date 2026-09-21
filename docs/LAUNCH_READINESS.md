# Launch Readiness — Telnyx 10DLC / TFV Registration

> **Status: NOT launch-ready.** Corrects `docs/TELNYX_IMPLEMENTATION_GAPS.md` (predates the
> filing and reconciliation code) and supersedes earlier drafts; the original is left
> unedited. Claims cite symbols in the audited files only. No secrets, no live carrier calls,
> and no live-carrier integration test yet — production Telnyx credentials remain an external
> prerequisite (§6). Integration evidence below is drawn from verified commits `ddbdd89`,
> `81df130`, `43e1ff9`, `907b829`, `1d95fbf`, `0719e59`, `9b9e261`, `ef724d8`.

## 0. Sources inspected

`api/routes/registration.py`, `api/routes/numbers.py`, `compliance/registration.py`,
`services/telnyx_brand_filing.py`, `services/telnyx_campaign_filing.py`,
`services/telnyx_tollfree_filing.py`, `services/telnyx_number_association.py`,
`providers/telnyx/registration.py`, `providers/telnyx/tollfree_verification.py`,
`providers/telnyx/tollfree_payload.py`, and the offline tests in §8. The TFV module
`services/telnyx_tollfree_filing.py` exposes `file_tollfree_verification_with_telnyx` and
`reconcile_tollfree_filing_with_telnyx`; the transport side exposes
`TelnyxTollfreeVerificationClient.list_requests`. Referenced but not supplied, therefore
**UNVERIFIED**: `provider_accounts`, `providers/telnyx/registration_status`
(`map_brand_status`, `map_campaign_status`, `APPROVED`), `brand_payload`/`campaign_payload`,
`config.Settings`, `models.numbers`, `auth.deps`.

## 1. Corrections to the gaps doc and earlier drafts

- A1/A2 ("store the Telnyx ref", "idempotency") were **NOT BUILT**; **now built** — filing
  writes `carrier_refs["telnyx"]` on success and commits a durable attempt marker first.
- Approval was operator-asserted; it is **now fail-closed** — an `approved` decision is
  accepted only after a fresh Telnyx GET confirms the matching record approved.
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
`app.state.telnyx_http_client`.

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
multiple, or mismatched results leave the marker in place and still require the runbook /
support workflow (§3.2, §4.2).

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
TFV's `carrier_refs["telnyx"]` (toll-free). Persisted rows only, no carrier call. Non-Telnyx
carriers keep allow-on-unknown unless `REQUIRE_NUMBER_REGISTRATION`.

**Transports / payload.** `TelnyxRegistrationClient`, `TelnyxTollfreeVerificationClient`, the
pure `build_tollfree_verification_payload`; single attempt, no auto-retry, no secrets logged.

## 3. Remaining work (code / product / external)

1. **Code — stale approval cannot be demoted; design unresolved.** Persisted local approval
   can become stale after a later Telnyx downgrade/suspension; send gating still trusts local
   terminal approval and has no way to demote it. The remediation is **not yet designed**: it
   should rely on **bounded freshness** (a recheck window) and/or **explicit revocation
   evidence consulted by the send gate**, while leaving the terminal registration status
   itself intact. A Telnyx query on every send is explicitly *not* the approach. Open code
   work.
2. **Product/ops (P0) — reconciliation runbook.** Zero/multiple/mismatched timeout
   reconciliation needs a written operator runbook and support workflow; hand-editing
   `carrier_refs`/markers as evidence must never be done casually.
3. **External — credentials and live verification.** Production Telnyx credentials, active
   provider account(s), and live sandbox/authorized verification are external prerequisites.
   No secrets belong in the repo or docs.
4. **Owner decisions.** Filing budget / current carrier fees, KYC scope, prepaid/settlement,
   the non-Telnyx `REQUIRE_NUMBER_REGISTRATION` default, BYON, pricing, and deploy/rollback.
5. **Confirm — order-route guard regression test.** Confirm whether a dedicated order-route
   guard regression test remains missing; if so it is **P1**, not P0.

## 4. P0 launch blockers (ordered)

1. **Stale approval trusted indefinitely (code).** Approval is verified only when recorded and
   the send path never re-checks the carrier, so a registration Telnyx later fails or suspends
   keeps sending locally, and terminal `approved` cannot be corrected; the bounded-freshness /
   revocation design is unresolved (§3.1).
2. **No reconciliation runbook / support workflow (product/ops).** Zero/multiple/mismatched
   results from `reconcile_tollfree_filing_with_telnyx` retain the marker and are unresolvable
   without a documented, safe operator process (§3.2).
3. **No real credentials / no live verification (external).** Production Telnyx credentials
   and an active provider account are external prerequisites, and no test exercises the real
   API (§3.3, §6, §8).

## 5. P1 (non-blocking hardening)

- Confirm and, if missing, add a dedicated route regression test for the Telnyx order-path
  guard (P1).
- Note that SQLite makes `with_for_update` a no-op (Postgres-only concurrency).
- Decide the `REQUIRE_NUMBER_REGISTRATION` default for non-Telnyx carriers.

## 6. Owner decisions, credentials and deploy (not code)

- Written authorization and budget for **billable, non-refundable** 10DLC filings; confirm the
  current fee/refund schedule and carrier number-association fees from the carrier's published
  terms (no amounts asserted here).
- Production Telnyx credentials / active provider account(s), provisioned and rotated outside
  the repo (never in docs or logs); filing and the guards cannot work without them.
- Whether carrier-confirmed approval suffices and who is accountable; a named process to
  reconcile timeout markers and re-check already-`approved` registrations (§4.1, §4.2).
- Per-environment `REQUIRE_NUMBER_REGISTRATION` default; KYC scope; prepaid/settlement policy;
  BYON decision; pricing pass-through.
- Deploy window and rollback owner; a migration only if `carrier_refs` is proven insufficient.

## 7. Security risks (beyond missing features)

- **Stale carrier evidence is trusted.** The guards run once and the send gate reads only
  persisted rows, so after a carrier downgrade a number keeps sending on a now-false
  `approved` — and terminal `approved` cannot be demoted.
- **Reconciliation dead-ends pressure operators to forge evidence.** Zero/multiple/mismatched
  results leave the marker and force a manual step; without a runbook the temptation is to
  hand-edit `carrier_refs`, out-of-band evidence the guard then trusts.
- **Unknown-registration allowance for non-Telnyx carriers:** allow-on-unknown is the default;
  the only tightening is an env flag, so the control is opt-in there.
- **Mock-only confidence.** A shape change in `registration_status`/`verificationStatus` can
  mis-map and silently approve.

## 8. Verification: offline tests done, live carrier still missing

Offline tests (real service + `httpx.MockTransport`; **no live Telnyx call**) are done and were
run on the integration branch. Evidence after the filing route landed: **319** Telnyx /
registration tests passed. After reconciliation landed: **224** tollfree/TFV-selected tests
passed and **52** route+reconciliation tests passed. **Ruff is clean** on the changed
production and test files. Coverage includes approval guards
(`test_telnyx_brand_approval_guard.py`, `test_telnyx_campaign_tfv_approval_guard.py`): missing
ref / carrier pending / mismatched id / wrong TFV field / confirmed approval / a repeated
approval now returning 409; number association (`..._success.py` + `..._failures.py`): success,
pending, timeout, wrong-phone and repeat markers; payload tests for field and enum
validation; TFV filing and reconciliation route tests.

**Still missing:** live-carrier verification (production credentials, §6), the reconciliation
operator runbook (P0, §4.2), and confirmation of a Telnyx order-path route regression test
(P1).
