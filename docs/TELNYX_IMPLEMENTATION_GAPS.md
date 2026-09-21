# Telnyx Carrier + 10DLC / TFV — Gap & Status Ledger

> **Audit artifact.** This document tracks implementation status for the Telnyx carrier,
> 10DLC, and toll-free verification (TFV) work. It is **not** implementation, **not** a
> deployment plan, and **not** a substitute for the code it describes. An item is only
> **BUILT** when it is marked so against a cited symbol or commit plus a `Verify:` command
> that reproduces the claim.
>
> **Status: NOT launch-ready.** The remaining P0 items in section 5 block production launch.
> No credentials, account identifiers, phone numbers, hostnames, or secrets are recorded
> here, by design.

## 0. Scope and method

- This edition **reconciles** the earlier pre-implementation map against the work that has
  since landed. Items the earlier map listed as **NOT BUILT** that are now implemented are
  moved into the **BUILT ledger** (section 3) with evidence, not deleted.
- **Evidence types:**
  - a **symbol** confirmed in a source file (path cited where known);
  - a **commit** SHA attributed by `git show --stat`;
  - a **`Verify:`** command (grep or test) that reproduces the claim.
- **Commit attributions for this edition** (from `git show --stat`):

  | Commit | Content |
  | --- | --- |
  | `ddbdd89` | Guarded 10DLC brand/campaign filing foundation — clients, payloads, routes, tests. |
  | `81df130` | Carrier-backed approval guards, number association, send/order gating, TFV payload. |
  | `43e1ff9` | Ignored / no-op status updates conflict. |
  | `907b829` | Guarded TFV filing **service** and tests. |
  | `1d95fbf` | TFV filing **route** and route tests. |
  | `0719e59` | TFV list / read transport. |
  | `9b9e261` | TFV reconciliation **service** and tests. |
  | `ef724d8` | TFV reconciliation **route** and tests. |
  | `87b63db` | Bounded approval evidence — pure `app.compliance.telnyx_approval`, approval-freshness setting, focused tests. |
  | `df849d3` | Enforce fresh approval evidence — atomic evidence on a confirmed `/status`, operator+compliance-guarded read-only POST refresh/revoke routes (carrier GET only), send-gate enforcement. |
  | `c6284e0` | Dedicated Telnyx order-route campaign guard regression test and the TFV reconciliation runbook. |
  | `c45bc0` | Fix stale plan-supplied messaging test fixture (`Org.telephony_prepaid=False`); production billing behavior unchanged, no assertion weakened. |
  | `a24d322` | `pg_only` two-session PostgreSQL brand-filing race test **harness** — `backend/tests/test_telnyx_postgres_concurrency.py` (mocked carrier transport); PostgreSQL execution not run here. |

- Where a symbol's exact spelling has drifted from the commits, the `Verify:` grep on the
  row is authoritative; trust the grep and update the row.
- Symbols are cited by name, not by line number, so this ledger does not drift when the
  source is reformatted.
- **Nothing outside the cited evidence is asserted here.** Where a path is listed without a
  **BUILT** tag, treat it as a location to *confirm before editing*.

## 1. Reading the tags

| Tag | Meaning |
| --- | --- |
| **BUILT** | Implemented and backed by a cited symbol/commit, with offline test evidence where testable. |
| **BUILT (operator-gated)** | Implemented behind `require_platform_operator` and a permission dependency. |
| **LOCAL-ONLY** | A value is written to our database but nothing drives it to a carrier (legacy path). |
| **PARTIAL** | Some of the behavior exists; the row states exactly what is still missing. |
| **NOT BUILT (audited)** | No code found for this; it may exist elsewhere — verify. |
| **BLOCKED** | Cannot proceed without an external action (credentials, authorization, decision). |
| **OWNER DECISION** | Requires a human/business decision, budget, or a production action. Block, do not guess. |
| **GATE** | A safety/ordering requirement; violating it can let a partial carrier submission be treated as approved. |
| **P0 / P1** | Priority. P0 blocks any production filing; P1 is required but not on the critical path. |

## 2. Historical decisions preserved from the pre-implementation map

These findings and decisions still stand and must not be re-litigated silently.

- **D-1. `carrier_refs` is the per-carrier reference store; no new column or migration is
  assumed.** `carrier_refs: Mapped[dict]` (backed by `PortableJSON`, `nullable=False`,
  `default=dict`) exists on `Brand`, `Campaign`, and `TollFreeVerification` in
  `backend/app/models/numbers.py`. The Telnyx filing work writes a `"telnyx"` entry into
  this existing field; a schema change is justified only if a later read proves
  `carrier_refs` insufficient.
- **D-2. `TollFreeVerification` stays a separate table from `Campaign`** (module docstring,
  phase-4-plan DR-4). The TFV work did not merge the tables.
- **D-3. The registration state machine is monotonic and terminal-sticky.**
  `advance_status`, `REGISTRATION_RANK`, `TERMINAL_REGISTRATION`, and `REGISTRATION_STATUSES`
  in `backend/app/services/registration.py` and `backend/app/models/numbers.py` are
  unchanged in intent: a terminal status cannot be demoted, `"rejected"` is terminal, and
  rank regressions are ignored.
- **D-4. Routes own the transaction.** Filing services mutate; the route performs
  `await ctx.session.commit()`. Every writer must route status changes through
  `advance_status` and must never assign `entity.status` directly.
- **D-5. The legacy `submit_*` service functions remain explicit LOCAL-ONLY paths.**
  `submit_brand`, `submit_campaign`, and `submit_tollfree` in
  `backend/app/services/registration.py` do **not** call Telnyx. They validate the entity and
  then `advance_status(<entity>, "submitted")` directly, moving local state to `submitted`
  with **no carrier HTTP call and no `carrier_refs["telnyx"]` write**. Carrier filing is
  performed only by the separate `/file-telnyx` routes and the `file_*_with_telnyx` services
  (section 3).
  > **Risk that must not be lost:** because the legacy path advances local state to
  > `submitted` on its own, a local `"submitted"` **does not** imply that a carrier reference
  > exists. Any reader that treats local `submitted` as carrier-submitted is wrong. The
  > standing invariant is:
  > `status ∈ {"approved", "rejected"}` **only if** a trusted writer advanced it there, and a
  > local `"submitted"` is never treated as carrier truth without a persisted carrier
  > reference written by the carrier filing path.
- **D-6. Terminal-aware state-machine assertion retained.** An earlier draft claimed a
  `rejected` entity could later be cleared. That is impossible because `"rejected"` is in
  `TERMINAL_REGISTRATION`; tests T4/T5/T5b below encode the terminal-aware behavior.
- **D-7. Billable operations stay unasserted in amount.** 10DLC brand/campaign filing is a
  billable carrier action and a rejection can forfeit the fee; **no amount, fee schedule,
  or refundability is asserted in this document** — confirm from the carrier's current
  published terms at decision time (section 7).

## 3. BUILT ledger (previously listed as unbuilt)

Each row states what is built, the evidence, how to verify it, and where it is covered
(section 6). Coverage is stated at the aggregate-run level only (see the note under section
6); no one-to-one test-name mapping is claimed, except where a specific test file is supplied.

| # | Item | Status | Evidence | Verify |
| - | ---- | ------ | -------- | ------ |
| F1 | Telnyx brand and campaign filing clients plus payload builders | **BUILT** | commit `ddbdd89` (guarded 10DLC brand/campaign filing foundation: clients, payloads, routes, tests) | `grep -rn "telnyx" backend/app/services backend/app/providers` |
| F2 | Durable attempt markers around the filing call | **BUILT** | commit `ddbdd89` foundation | `grep -rn "attempt" backend/app/services backend/app/models` |
| F3 | Exact returned carrier ID/reference persistence — brand and campaign persist the carrier ID/reference the carrier returned; TFV persists a request ID | **BUILT** | commits `ddbdd89` (brand/campaign), `907b829` + `1d95fbf` (TFV) | `grep -rn "carrier_refs" backend/app/services` |
| F4 | **Separate** carrier filing path: `/file-telnyx` routes and `file_*_with_telnyx` services write `"submitted"` only on carrier acceptance and persist `carrier_refs["telnyx"]` | **BUILT** | commits `ddbdd89` (brand/campaign), `907b829` + `1d95fbf` (TFV) | `grep -rn "file-telnyx\|with_telnyx" backend/app/api/routes/registration.py backend/app/services` |
| F5 | Operator-gated filing routes (`require_platform_operator` plus `require_permission("compliance:manage")`) | **BUILT (operator-gated)** | brand/campaign routes from `ddbdd89`; TFV route from `1d95fbf` | `grep -n "require_platform_operator\|compliance:manage" backend/app/api/routes/registration.py` |
| F6 | Carrier-confirmed brand/campaign/TFV approval with **exact id matching** against `carrier_refs["telnyx"]`; a local write cannot self-assert approval | **BUILT** | commit `81df130` (carrier-backed approval guards) — TFV filing commits did not introduce approval guards | `grep -rn "carrier_refs" backend/app/services backend/app/api/routes` |
| F7 | Ignored / no-op status updates return a **conflict** instead of reporting success | **BUILT** | commit `43e1ff9` | `grep -rn "advance_status" backend/app/api/routes/registration.py` |
| F8 | Telnyx number-to-campaign carrier association plus a durable association marker on the number | **BUILT** | commit `81df130` (number association) | `grep -rn "campaign_id\|associat" backend/app/services backend/app/models/numbers.py` |
| F9 | Order-time campaign guard — a number cannot be ordered onto a campaign that is not carrier-approved | **BUILT** | commit `81df130` (order gating); dedicated route regression test commit `c6284e0` | `backend/tests/test_telnyx_order_campaign_guard.py` |
| F10 | Fail-closed Telnyx send gate — send is refused when association or approval evidence is missing | **BUILT** | commit `81df130` (send gating); fresh-evidence enforcement added by `df849d3` | `grep -rn "can_send\|send_gate" backend/app` |
| F11 | TFV filing: guarded **service** plus strict operator **route** | **BUILT (operator-gated)** | service `907b829`; route `1d95fbf` | `grep -rn "telnyx_tollfree_filing\|with_telnyx" backend/app/services/telnyx_tollfree_filing.py backend/app/api/routes/registration.py` |
| F12 | `TfvOut` / `_tfv_out` exposes `carrier_refs` (fixes the earlier `TfvOut` inconsistency) | **BUILT** | commit `1d95fbf` (route wiring) | `grep -n "carrier_refs" backend/app/api/routes/registration.py backend/app/models/numbers.py` |
| F13 | Official list / read transport for carrier state reads (GET) | **BUILT** | commit `0719e59` (TFV list/read transport) | `grep -rn "list\|\.get(" backend/app/providers/telnyx/tollfree_verification.py` |
| F14 | Exact-match, **GET-only** timeout reconciliation service | **BUILT** | commit `9b9e261` (TFV reconciliation service + tests) | `grep -rn "reconcil" backend/app/services` |
| F15 | Timeout reconciliation operator route | **BUILT (operator-gated)** | commit `ef724d8` (TFV reconciliation route + tests) | `grep -rn "reconcil" backend/app/api/routes` |
| F16 | No automatic POST retries after an ambiguous or timeout outcome; ambiguous records stay blocked | **BUILT** | commits `9b9e261`, `ef724d8` | acceptance criteria T20, T24 in section 6 |
| F17 | No local approval in filing or reconciliation — reconciliation can only confirm what the carrier reports | **BUILT** | commits `907b829`, `9b9e261`, `ef724d8` | acceptance criteria T19, T21 in section 6 |
| F18 | Legacy `submit_brand` / `submit_campaign` / `submit_tollfree` remain **LOCAL-ONLY** and do **not** call Telnyx | **BUILT (legacy, LOCAL-ONLY)** | `backend/app/services/registration.py` | `grep -n "submit_brand\|submit_campaign\|submit_tollfree" backend/app/services/registration.py` |
| F19 | Pure bounded-approval-evidence module `app.compliance.telnyx_approval`; evidence stored at `carrier_refs["telnyx_approval"]`, bound to the **exact** carrier id, with state `approved`/`revoked`, a UTC `checked_at`, and a `source` of `status_decision`/`refresh` | **BUILT** | commit `87b63db` (bounded approval evidence) | `grep -rn "telnyx_approval" backend/app` |
| F20 | Approval-freshness bound `TELNYX_APPROVAL_MAX_AGE_SECONDS` (default `604800`; validated in the inclusive range `300..2592000`; no disable) | **BUILT** | commit `87b63db` | `grep -rn "TELNYX_APPROVAL_MAX_AGE_SECONDS" backend/app` |
| F21 | First carrier-confirmed `approved` `/status` writes approval evidence **atomically** with the status advance | **BUILT** | commit `df849d3` (enforce fresh approval evidence) | `grep -rn "telnyx_approval" backend/app/api/routes/registration.py` |
| F22 | Operator+compliance guarded **read-only POST** `refresh-telnyx` routes (brand/campaign/TFV) whose **only carrier interaction is a GET**; they refresh or revoke approval evidence **without changing the terminal local status**; a carrier error or mismatched carrier id mutates nothing | **BUILT (operator-gated)** | commit `df849d3` | `grep -rn "refresh-telnyx" backend/app/api/routes/registration.py` |
| F23 | Telnyx campaign/TFV send gate requires **fresh, matching, `approved`** approval evidence; there is **no network call and no new carrier query** on the send path; non-Telnyx behavior is unchanged | **BUILT** | commit `df849d3` | `grep -rn "telnyx_approval" backend/app/compliance` |
| F24 | Dedicated Telnyx order-route campaign guard regression test | **BUILT** | commit `c6284e0` | `backend/tests/test_telnyx_order_campaign_guard.py` |
| F25 | TFV reconciliation operator runbook | **BUILT (documentation deliverable)** | commit `c6284e0` | `docs/runbooks/TELNYX_TFV_RECONCILIATION.md` |

Key TFV module paths referenced above:

- `backend/app/services/telnyx_tollfree_filing.py` — TFV filing service (F11, `907b829`).
- `backend/app/providers/telnyx/tollfree_verification.py` — Telnyx TFV provider
  transport/payload (F13, `0719e59`).
- `backend/app/api/routes/registration.py` — operator-gated filing and reconciliation routes,
  plus operator+compliance-guarded read-only POST approval refresh/revoke routes whose only
  carrier interaction is a GET (F4, F5, F11, F12, F15, F22).
- `backend/app/compliance/telnyx_approval.py` — bounded approval evidence (F19–F23, `87b63db`).
- `backend/tests/test_telnyx_postgres_concurrency.py` — `pg_only` PostgreSQL concurrency race
  test **harness** (P1-V2, `a24d322`); with PostgreSQL unavailable it is skipped and never
  executed here, so it does not verify behavior.
- `docs/runbooks/TELNYX_TFV_RECONCILIATION.md` — operator runbook for TFV filing
  reconciliation and controlled marker repair (P0-O1, documentation deliverable, `c6284e0`;
  the named owner and audit-trail assignment remain an `OWNER DECISION`).

**Explicitly retired claims.** This ledger **no longer** claims that any of the following
are unbuilt: carrier clients; filing writers; carrier-confirmed approval; number
association; TFV reference visibility; timeout reconciliation; bounded approval evidence;
approval refresh/revocation; a dedicated order-route guard test. It also **no longer** claims
that the send gate trusts approval indefinitely or cannot represent a carrier downgrade — a
downgrade is now expressed as separate revoked/stale approval evidence (F19–F23) while the
terminal status is left intact. It **does** keep the legacy `submit_*` paths flagged as
`LOCAL-ONLY` (F18, D-5) and does **not** claim they call the carrier.

## 4. State machine and approval semantics (kept, now locked by tests)

- `advance_status(entity, new_status, *, error=None) -> bool` —
  `backend/app/services/registration.py`. Unknown statuses raise `ValidationFailedError`;
  `None` is treated as `"draft"`; a `new_status == current` no-op returns `False`; changes
  away from a terminal status are ignored; rank regressions are ignored; `last_error` is set
  only when advancing to `"rejected"` and cleared on any other successful advance. Logs
  `registration_status_advanced`, `registration_status_regression_ignored`, and
  `registration_terminal_status_ignored`.
- `REQUIRED_BRAND_FIELDS`, `REQUIRED_CAMPAIGN_FIELDS`, `validate_brand_for_submission`,
  `validate_campaign_for_submission`, `submit_brand`, `submit_campaign`, `submit_tollfree`,
  and `numbers_on_campaign` remain in `backend/app/services/registration.py`. An EIN is
  required unless `entity_type == "SOLE_PROPRIETOR"`, and `submit_campaign` requires the
  parent brand to be `status == "approved"`. `numbers_on_campaign` is a **local count**; the
  carrier association lives in the number-association path (F8).
- `REGISTRATION_STATUSES`, `REGISTRATION_RANK`, `TERMINAL_REGISTRATION`, `can_send`, and
  `carrier_refs` — `backend/app/models/numbers.py`.
- Operator status routes `set_brand_status`, `set_campaign_status`, and `set_tfv_status`
  remain in `backend/app/api/routes/registration.py`, each gated by
  `Depends(require_platform_operator)` **and**
  `Depends(require_permission("compliance:manage"))`. On invocation each performs a fresh
  carrier read and then applies the carrier-confirmed decision (F6/F7); there is **no
  webhook and no poller** claimed here — the operator trigger is what admits the change.
  Every writer routes through `advance_status`; no writer assigns `entity.status` directly.
- **Terminal approval vs. separate approval evidence.** A terminal local `approved` is
  **intentionally not demoted** (D-3). Bounded evidence lives in
  `carrier_refs["telnyx_approval"]` (`backend/app/compliance/telnyx_approval.py`, `87b63db`),
  bound to the **exact** carrier id, with state `approved`/`revoked`, a UTC `checked_at`, and a
  `source` of `status_decision`/`refresh`. Freshness is bounded by
  `TELNYX_APPROVAL_MAX_AGE_SECONDS` (default `604800`; validated `300..2592000`; no disable).
  The first carrier-confirmed `approved` `/status` writes the evidence atomically with the
  status advance (`df849d3`), and operator+compliance guarded **read-only POST**
  `refresh-telnyx` routes — whose only carrier interaction is a GET — refresh or revoke the
  evidence without touching the terminal status. The Telnyx campaign/TFV send gate requires
  fresh, matching, `approved` evidence and does **no** carrier query on the send path; a
  downgrade is therefore expressed as revoked/stale evidence while the status stays
  `approved`.

## 5. Remaining gaps (prioritized)

### P0 — blocking

**P0-E1 (external). Real Telnyx credentials, account, and profile setup plus authorized
live verification.** The test suite is `httpx.MockTransport` only; no live carrier call has
been made. `BLOCKED`.

- Requires production credentials, messaging profile(s), and explicit authorization to make
  live calls (`OWNER DECISION`, section 7). Credentials are provisioned outside the
  repository; nothing about them is recorded here.
- `Test:` a single, explicitly authorized live smoke verification against a non-production
  entity, recorded as a manual audit note.

**P0-O1 (operational). Reconciliation runbook — owner / audit-trail / support workflow.** The
timeout/late-approval reconciliation service and route (F14/F15) can legitimately return
**zero**, **multiple**, or **mismatched** carrier records, and durable attempt markers (F2)
can be left inconsistent after a crash or partial outage. The **written runbook is DELIVERED**
(`docs/runbooks/TELNYX_TFV_RECONCILIATION.md`, commit `c6284e0`); **the assigned owner,
audit-trail storage, and Telnyx support workflow remain `OWNER DECISION`.**

- Delivered: `docs/runbooks/TELNYX_TFV_RECONCILIATION.md` — a case-by-case operator runbook
  for zero records, multiple records, a mismatched record, carrier timeout/error, a malformed
  marker, a marker without a request id, a request id without a marker, and already-resolved
  records. It states the "never blindly retry the POST" safety rule, the guarded
  `POST /tollfree/{tfv_id}/reconcile-telnyx` path, a controlled marker-repair procedure
  (two-person approval, backup/snapshot, exact carrier evidence, audit-log fields, rollback,
  post-repair verification), an evidence-handling rule, an escalation path, an incident/audit
  template, prohibitions, a verification checklist, and repository symbol/route references.
- Still required (`OWNER DECISION`): assign the **named owner** for that runbook, the
  **audit-trail** storage location (what was changed, by whom, why, and the carrier evidence
  relied on), and the Telnyx support workflow. Until then P0-O1 is not fully closed.
- `Verify:` the runbook file exists (`docs/runbooks/TELNYX_TFV_RECONCILIATION.md`) and names
  its role owners (Platform Operator / Incident Commander); the accountable owner assignment
  is still open;
  `grep -rn "marker" docs backend/app/services`.

> **Former P0-C1 (stale approval / revocation) is now BUILT** — see F19–F23 (`87b63db`,
> `df849d3`). Bounded approval evidence plus operator+compliance-guarded read-only POST
> refresh/revoke routes (carrier GET only), consulted by the send gate, close the code gap. It
> is no longer listed as a blocking code gap; the residual risk is the freshness-window policy
> in section 7.

### P0 / P1 — owner decisions (each blocks its area; see section 7)

- **P0-D1.** Spend authorization and current fees for billable brand/campaign filing.
- **P0-D2.** Prepaid / settlement policy and where it is enforced (before the billable call).
- **P0-D6.** Deploy window, rollback plan, and rollback owner.
- **P0-D7.** Non-Telnyx unknown-registration policy: what happens when a registration exists
  at the carrier with no matching local entity, and the reverse.
- **P1-D3.** KYC enablement scope.
- **P1-D4.** BYON (bring-your-own-number) support and the ownership checks that run before
  any campaign attach.
- **P1-D5.** Pricing pass-through (if any) for billable registrations.

### P1 — verification

- **P1-V2 (production PostgreSQL concurrency verification).** The test suite runs on SQLite,
  which ignores `SELECT … FOR UPDATE`, so lock/claim behavior used by filing, number
  association, and reconciliation is unverified under real concurrency. The PostgreSQL **test
  harness** now exists (`backend/tests/test_telnyx_postgres_concurrency.py`, commit `a24d322`)
  — a `pg_only`, real two-session brand-filing race test with a mocked carrier transport — but
  with PostgreSQL unavailable it is skipped and has **never executed against PostgreSQL**, so
  the race outcome is **unverified** and **P1-V2 is not closed**. A harness is not verified
  behavior. `Verify:` run the test against PostgreSQL and assert single-winner behavior.

## 6. Acceptance test matrix

These are **acceptance criteria**, not a verified one-to-one map to the existing suite.
Historical aggregate runs for earlier passes: **319 Telnyx/registration tests passed**, **224
tollfree/TFV-selected tests passed**, **52 route/reconciliation tests passed**, **Ruff
clean**. For the bounded-approval / revocation pass and the order-route guard, targeted runs
reported **103** evidence/config tests passed, **37** existing affected Telnyx registration
tests passed, and **29** new refresh/send-gate tests passed, with **Ruff and diff checks
clean**; the dedicated order-route guard test file
`backend/tests/test_telnyx_order_campaign_guard.py` was added (commit `c6284e0`).

The latest broad selector run (`pytest backend/tests -q -k "telnyx or registration"`) reported
**588 passed, 1 skipped, 2402 deselected, zero failures (177.94 seconds)**. The single skip is
`backend/tests/test_telnyx_postgres_concurrency.py`, skipped because PostgreSQL is unavailable
on this machine; commit `a24d322` built that `pg_only` **harness**, but the real PostgreSQL
execution is **NOT RUN**, so P1-V2 remains open. Commit `c45bc0` fixes a stale plan-supplied
messaging test fixture (sets `Org.telephony_prepaid=False`) with production billing behavior
unchanged. No individual test name below was matched to a specific assertion in this pass; the
matrix records the behavior each row must protect.

Names are suggestions; place them beside the existing tests that import
`app.services.registration`. Coverage column values:

- **built behavior** — the code path exists in the BUILT ledger; covered by the aggregate
  runs above (exact case names not asserted here).
- **target** — behavior is not built (or not yet regression-tested); the criterion is a
  target, not a pass.

| # | Test (suggested) | Setup | Assertion | Coverage |
| - | ---- | ----- | --------- | -------- |
| T1 | `unknown_status_rejected` | any entity | `advance_status(e, "bogus")` raises `ValidationFailedError` | built behavior |
| T2 | `none_status_is_draft` | unflushed entity | advancing to `"submitted"` succeeds from `None` | built behavior |
| T3 | `late_submitted_does_not_demote_approved` | status `approved` | advancing to `"submitted"` leaves status `approved`, returns `False` | built behavior |
| T4 | `terminal_status_is_sticky` | status `approved` | advancing to `"rejected"` is ignored and `last_error` is unchanged | built behavior |
| T5 | `rejected_sets_last_error` | status `submitted` | advancing to `"rejected"` with an error stores that error in `last_error` | built behavior |
| T5b | `rejected_is_terminal` | status `rejected` with error | advancing to `"approved"` is ignored; status and `last_error` unchanged | built behavior |
| T6 | `campaign_requires_approved_brand` | brand `submitted` | `submit_campaign` raises `ValidationFailedError` | built behavior |
| T7 | `submit_terminal_conflicts` | brand `approved` | `submit_brand` raises `ConflictError`, status unchanged | built behavior |
| T8 | `status_route_requires_operator` | non-operator caller | `/status` is refused and status is unchanged | built behavior |
| T9 | `failed_carrier_call_is_not_submitted` | simulated carrier error on the `/file-telnyx` path | entity is **not** `"submitted"` and no `carrier_refs["telnyx"]` is set | built behavior (F1–F4) |
| T10 | `resubmit_is_idempotent` | existing `carrier_refs["telnyx"]` | one carrier create and a stable reference | built behavior (F2–F3) |
| T11 | `unverified_source_is_rejected` | missing or invalid credentials on the automated writer | no status change | built behavior (F5) |
| T12 | `unknown_reference_noop` | unmatched `carrier_refs` value | no status change, no crash | built behavior (F6) |
| T13 | `approval_replay_is_noop` | same `approved` transition twice | the second is a no-op | built behavior |
| T14 | `number_attach_requires_approved_campaign` | campaign `submitted` | attach is refused | built behavior (F8–F10) |
| T15 | `approval_requires_exact_carrier_id` | carrier decision with a near-match or other id | status is not advanced and no `carrier_refs["telnyx"]` is overwritten | built behavior (F6) |
| T16 | `ignored_status_write_is_conflict` | a write `advance_status` would ignore | the route returns a conflict, not success | built behavior (F7) |
| T17 | `number_association_is_carrier_confirmed` | number associated to a campaign | durable association marker present, set only from carrier-confirmed state | built behavior (F8) |
| T18 | `send_gate_fails_closed` | number with missing or unconfirmed association | the Telnyx send is refused | built behavior (F10) |
| T19 | `reconciliation_never_approves_locally` | reconciliation finds no carrier record | entity is unchanged and remains blocked | built behavior (F14–F17) |
| T20 | `timeout_reconciliation_is_get_only` | timeout on a submission | reconciliation uses GET/list transport only; no POST retry | built behavior (F13, F16) |
| T21 | `ambiguous_record_stays_blocked` | timeout with a non-matching carrier record | the record stays blocked; no local status advance | built behavior (F14–F16) |
| T22 | `tfv_out_exposes_carrier_refs` | TFV with a Telnyx reference | `TfvOut` returns `carrier_refs["telnyx"]` | built behavior (F12) |
| T23 | `tfv_filing_route_is_strict` | operator route with invalid input | refused; nothing written to the carrier or the entity | built behavior (F11) |
| T24 | `no_auto_post_retry_after_timeout` | timeout on a POST | no second POST is issued automatically | built behavior (F16) |
| T25 | `terminal_approval_stops_sending_after_revocation` | terminal-approved entity, carrier approval later withdrawn | the send gate refuses on revoked/stale evidence; the terminal status is unchanged | built behavior (F19–F23) |
| T26 | `order_route_campaign_guard` | order path with a non-carrier-approved campaign | the order is refused | built behavior (F24; `backend/tests/test_telnyx_order_campaign_guard.py`) |
| T27 | `postgres_single_winner_on_claim` | PostgreSQL, concurrent claim/reconcile | exactly one winner; no duplicate carrier write | **target** (P1-V2: `pg_only` harness added `a24d322`, but it is skipped without PostgreSQL and not executed — unverified) |
| T28 | `approval_evidence_written_on_confirmed_approval` | first carrier-confirmed `approved` `/status` | `carrier_refs["telnyx_approval"]` is written atomically (state `approved`, exact id, UTC `checked_at`, `source="status_decision"`) | built behavior (F19–F21) |
| T29 | `refresh_route_revokes_evidence_without_status_change` | operator+compliance read-only POST `refresh-telnyx` (carrier GET only) on a terminal-approved entity | evidence moves to `revoked` (or refreshes); the terminal status is unchanged; a carrier error / mismatched id mutates nothing | built behavior (F22) |
| T30 | `stale_evidence_blocks_send` | approval evidence older than `TELNYX_APPROVAL_MAX_AGE_SECONDS` | the Telnyx campaign/TFV send gate refuses; no carrier query is made | built behavior (F20, F23) |

Tests marked **built behavior** are protected by code paths in the BUILT ledger and the
aggregate runs above. The test marked **target** (T27) is a criterion for behavior that is not
yet verified against PostgreSQL; the `pg_only` harness exists (commit `a24d322`) but is skipped
without PostgreSQL and has not been executed, so it is explicitly **not** passing on SQLite.

## 7. Owner decisions and prerequisites (blockers)

- Authorization to file **billable** 10DLC brand/campaign applications, plus a
  rejection-cost budget. Confirm the fee schedule and refundability from an authoritative
  carrier source at decision time; **no amount is asserted here**.
- Production carrier **credentials**, account and profile setup, and messaging profile(s),
  provisioned outside the repository, plus explicit authorization for a single live
  verification (P0-E1).
- The **approval-freshness window** policy: whether the `TELNYX_APPROVAL_MAX_AGE_SECONDS`
  default (`604800`) and validated range (`300..2592000`) are acceptable, and who is
  accountable for operating the read-only POST refresh/revoke routes (whose only carrier
  interaction is a GET).
- **Prepaid / settlement** policy and the enforcement point (before the billable call).
- **KYC** enablement scope.
- **Pricing** pass-through (if any) for billable registrations.
- **BYON** support decision and the ownership checks that run before any campaign attach.
- **Non-Telnyx unknown-registration** policy (a carrier record with no local entity, and the
  reverse).
- **Deploy** window, rollback plan, and a rollback owner.
- A **named owner** (role assignment), an **audit-trail** storage location, and the Telnyx
  **support workflow** for the reconciliation runbook (P0-O1) — the runbook document itself
  is delivered; the accountable owner and audit trail are still open.

## 8. Non-goals for the implementer

- No live carrier calls, deployments, or git operations as part of this document.
- No credentials, hostnames, account identifiers, phone numbers, or secrets recorded here.
- Do not mark any item **BUILT** without citing a symbol or commit plus a `Verify:` command.

## 9. Open questions

1. What is the acceptable carrier-state **freshness** window for the send gate? It is now a
   setting, `TELNYX_APPROVAL_MAX_AGE_SECONDS` (default `604800`; validated `300..2592000`; no
   disable). Is that default/range sufficient, and is a revocation record sufficient, or is a
   signed carrier notification required?
2. Is a single live verification (P0-E1) sufficient evidence to authorize production
   filing, or is a staged pilot required?
3. Who owns the reconciliation runbook, and where is the audit trail stored (P0-O1)? The
   runbook document is delivered; this owner/audit-trail/support-workflow question remains
   open.
4. Which code paths rely on `SELECT … FOR UPDATE`, and has each been exercised against
   PostgreSQL (P1-V2)? The `pg_only` harness (`a24d322`) exists but is skipped without
   PostgreSQL and has not been executed, so this question is not yet answered.
5. Should the legacy LOCAL-ONLY `submit_*` paths (D-5, F18) be retired, guarded, or left in
   place with a warning, so a local `submitted` is never mistaken for carrier-submitted?
