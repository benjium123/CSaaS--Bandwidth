# Telnyx Carrier + 10DLC / TFV — Implementation Gaps & Readiness Map

> **Audit artifact.** This document coordinates implementation work across delegates.
> It is **not** implementation, **not** a deployment plan, and **not** a substitute for
> the code it describes. Nothing here is "done" unless it is marked **EXISTS** against a
> cited symbol in a source file. No credentials, hostnames, account identifiers, or
> secrets are recorded here, by design.

## 0. Scope and method

- **Audited sources for this pass:**
  - `backend/app/services/registration.py`
  - `backend/app/api/routes/registration.py`
  - `backend/app/models/numbers.py`
- Every **EXISTS** claim is traceable to a symbol in one of the three files above.
- **Everything else in the repository is UNVERIFIED here.** Where a path is listed
  without the EXISTS tag, treat it as a location to *confirm before editing*, not as a
  statement that the file or symbol exists.
- No line numbers are cited; symbols are cited by name so this map does not drift when
  the source is reformatted.

## 1. Reading the tags

| Tag | Meaning |
| --- | --- |
| **EXISTS** | Behavior confirmed in one of the audited sources. |
| **LOCAL-ONLY** | A value is written to our database but nothing drives it to or reads it at a carrier. |
| **NOT BUILT (audited)** | No code in the audited sources performs this. It may exist elsewhere — *verify*. |
| **UNRESOLVED** | Cannot be settled from the audited sources; needs a human or a further code read. |
| **OWNER DECISION** | Requires a human/business decision, budget, or a production action. Block, do not guess. |
| **GATE** | A safety/ordering requirement; violating it can let a partial carrier submission be treated as approved. |

## 2. Confirmed existing behavior (EXISTS)

### 2.1 State machine — `backend/app/services/registration.py`

- `advance_status(entity, new_status, *, error=None) -> bool` — monotonic, rank-gated
  transition. It:
  - rejects unknown statuses with `ValidationFailedError`;
  - treats a `None` status as `"draft"`;
  - no-ops when `new_status == current` (returns `False`);
  - **ignores any change away from a terminal status** (`TERMINAL_REGISTRATION`);
  - **ignores rank regressions** (`REGISTRATION_RANK`);
  - sets `entity.last_error = (error or None)` **only when advancing to `"rejected"`**,
    and sets it to `None` on any other successful advance;
  - logs `registration_status_advanced` / `registration_status_regression_ignored` /
    `registration_terminal_status_ignored`.
- `REQUIRED_BRAND_FIELDS = ("name", "email", "street", "city", "state", "postal_code")`
  and `REQUIRED_CAMPAIGN_FIELDS = ("name", "use_case", "description", "opt_in_process")`.
- `validate_brand_for_submission(brand)` — required fields; an EIN is required unless
  `entity_type == "SOLE_PROPRIETOR"`.
- `validate_campaign_for_submission(campaign)` — required fields; at least one
  `sample_messages` entry; `opt_out_message` required.
- `submit_brand(session, brand_id)` — `NotFoundError` if missing, `ConflictError` if the
  entity is already terminal, validates, then `advance_status(brand, "submitted")`.
- `submit_campaign(session, campaign_id)` — 404/conflict handling, **requires the parent
  brand to be `status == "approved"`**, validates, then
  `advance_status(campaign, "submitted")`.
- `submit_tollfree(session, tfv_id)` — 404/conflict handling, requires `business_name`,
  `use_case`, `use_case_summary`, `opt_in_process`, then
  `advance_status(tfv, "submitted")`.
- `numbers_on_campaign(session, campaign_id) -> int` — a **local count** of `OrgNumber`
  rows where `campaign_id` matches.

**None of the `submit_*` functions performs an outbound carrier call.** Each only mutates
`status` on our own entity and returns it. See section 3.

### 2.2 Approval writer today — `backend/app/api/routes/registration.py`

- Router `router` with prefix `/api/v1/registration`, tag `registration`.
- `set_brand_status`, `set_campaign_status`, `set_tfv_status` — explicit, per-entity
  **platform-operator** endpoints at `/brands/{brand_id}/status`,
  `/campaigns/{campaign_id}/status`, `/tollfree/{tfv_id}/status`.
  - Each is gated by `Depends(require_platform_operator)` **and**
    `Depends(require_permission("compliance:manage"))`.
  - Each loads the entity, raises `NotFoundError` if absent, calls
    `reg.advance_status(<entity>, payload.status, error=payload.error)`, then commits.
  - `set_campaign_status`'s docstring states this is how a registrar decision is
    recorded, and that the monotonic gate prevents a stale `submitted` from demoting an
    `approved` campaign.
- **So the current approval source is operator input through these `/status` routes**
  (an authenticated platform operator posts a status). It is **not** a webhook, and this
  document does **not** presume one. A future **verified** source — whether a signed
  webhook **or** a verified poll — would be an additional legitimate writer, subject to
  the gates in section 5.
- `create_brand`, `create_campaign`, `create_tfv` mutate + `commit()`, mapping
  `IntegrityError` to `ConflictError`. `submit_brand`, `submit_campaign`, `submit_tfv`
  call the `reg.submit_*` service and then `await ctx.session.commit()` — i.e. **the route
  owns the transaction**, confirming the service functions are mutation-only.
- `create_tfv` refuses a non-toll-free number with `ValidationFailedError`, directing the
  caller to a 10DLC campaign instead.

### 2.3 Carrier references already exist — `backend/app/models/numbers.py`

- `REGISTRATION_STATUSES = ("draft", "submitted", "approved", "rejected")`,
  `REGISTRATION_RANK = {"draft": 0, "submitted": 10, "approved": 20, "rejected": 20}`,
  `TERMINAL_REGISTRATION = frozenset({"approved", "rejected"})`, and
  `can_send(status) -> bool` (true only for `"approved"`).
- **`carrier_refs: Mapped[dict]` (backed by `PortableJSON`, `nullable=False,
  default=dict`) already exists on `Brand`, `Campaign`, and `TollFreeVerification`.**
  The `Brand` docstring describes it as the mechanism for "one brand, many
  registrations", with the example shape
  `{"bandwidth": "BXXXX", "telnyx": "..."}`.
- **Therefore a new external-id column and its migration are NOT necessarily required.**
  `carrier_refs` is the existing place to store a per-carrier id. The open gap is that
  nothing in the audited sources *writes* a `telnyx` entry into it or *resolves* an
  inbound reference back to an entity.
- `TollFreeVerification` is a separate table from `Campaign` and is deliberately not
  merged (module docstring, phase-4-plan DR-4).
- `_brand_out`/`BrandOut` and `_campaign_out`/`CampaignOut` surface `carrier_refs` and a
  computed `missing_for_submission` list. **`TfvOut`/`_tfv_out` does NOT expose
  `carrier_refs`** (confirmed inconsistency), so a Telnyx ref on a TFV would be invisible
  through the current API.

## 3. Central finding: "submitted" is local, and "approved" is asserted, not confirmed

- `submit_brand`, `submit_campaign`, and `submit_tollfree` end in
  `advance_status(<entity>, "submitted")`. There is **no** carrier HTTP call, **no**
  `carrier_refs` write, **no** idempotency key, and **no** commit inside the service
  (the route commits).
  > A brand, campaign, or TFV marked `submitted` in our database is **not** evidence that
  > anything reached the carrier.
- The only writers of `"approved"` / `"rejected"` in the audited sources are the
  platform-operator `/status` routes, which trust the caller's posted value. There is no
  code that verifies a carrier decision before recording it, and approval does not require
  a `telnyx` entry in `carrier_refs`.
- **Consequence / risk:** an operator (or a future automation bug) can set `"approved"`
  with no carrier evidence, and the monotonic gate then makes that decision permanent
  (`approved` is terminal, so it cannot be walked back and a later `rejected` is ignored).
  Meanwhile any UI or billing surface reading `status` is reading a value we asserted,
  not a value the carrier confirmed.

## 4. Gap checklist

Each item has a `Verify:` (how to confirm current reality) and a `Test:` (acceptance test
or target). Do not treat an item as satisfied until its test exists and passes.

### A. Carrier submission linkage

- [ ] **A1. Store the Telnyx reference.** `EXISTS` storage, `NOT BUILT (audited)` writer.
  - `carrier_refs` already exists on all three models (section 2.3). No new column or
    migration is required *unless* a later read proves otherwise.
  - `Verify:` grep the repository for any writer of `carrier_refs` (especially a
    `"telnyx"` key) — none exists in the audited sources.
  - `Test:` a successful Telnyx submission records a `carrier_refs["telnyx"]` value that
    is unique per entity and stable across retries.
- [ ] **A2. Idempotency.** `NOT BUILT (audited)`.
  - A retried submission (after a timeout = "unknown state") must not create a second
    carrier object. Key it by an idempotency key or by reusing an existing
    `carrier_refs["telnyx"]`.
  - `Test:` calling a real submit twice results in exactly one carrier create and a
    stable `carrier_refs["telnyx"]`.
- [ ] **A3. Distinguish local "submitted" from carrier-submitted.** `GATE`.
  - `advance_status` is monotonic, so once `"submitted"` is written locally it cannot be
    lowered. A local write must therefore happen **only after** carrier acceptance, or the
    entity needs a pre-status that never advances on failure.
  - `Test:` when the carrier call fails, the entity is **not** left in `"submitted"` and
    no `carrier_refs["telnyx"]` is written.
- [ ] **A4. Transaction ownership.** `EXISTS`.
  - The `/status` and `/submit` routes call `await ctx.session.commit()`; the service
    functions mutate only. `Verify:` any new caller does the same and maps
    `ValidationFailedError` / `ConflictError` to 4xx (the existing routes rely on the
    framework's error handlers — confirm those mappings exist).
  - `Test:` a failed submit persists a rollback (no partial state).

### B. Approval source

- [ ] **B1. Current approval path.** `EXISTS` — the platform-operator `/status` routes
  (section 2.2). This is manual, trusted input.
- [ ] **B2. Verified automated source (optional, future).** `NOT BUILT (audited)`.
  - A future approval source may be a **signed webhook or a verified poll** — do not
    mandate one mechanism. Whichever is chosen must authenticate the caller before any
    status change. `OWNER DECISION`: which mechanism, and which signing secret /
    credential, provisioned where.
  - `Test:` an unauthenticated or incorrectly-signed automated call is rejected and
    changes no status.
- [ ] **B3. Reference → entity mapping.** `NOT BUILT (audited)` for automated input.
  - An automated source must resolve the entity via `carrier_refs` (A1); a reference that
    cannot be mapped must not mutate anything.
  - `Test:` an unknown reference is a logged no-op, not a 500 that retries forever.
- [ ] **B4. Route every status change through `advance_status`.** `GATE`.
  - The `/status` routes already do this; any new writer must too, and must never assign
    `entity.status` directly. This is what stops a retried, out-of-order `submitted` from
    demoting an `approved` entity.
  - `Test:` deliver `approved` then a late `submitted`; the final status stays `approved`
    (`registration_status_regression_ignored`).
- [ ] **B5. Replay safety.** `GATE`.
  - The rank gate already no-ops duplicates; assert it explicitly for a new source.
  - `Test:` delivering `approved` twice leaves status `approved` and the second delivery
    is a no-op.

### C. State machine (keep; lock it with tests)

- [ ] **C1.** `EXISTS` — `advance_status` rank and terminal guards.
  - `Test:` unknown status raises `ValidationFailedError`.
  - `Test:` a `None` status is treated as `"draft"`.
  - `Test:` advancing out of a terminal status to a *different* terminal status is
    ignored (for example `approved` → `rejected`).
  - `Test:` advancing to `"rejected"` stores the supplied `error` in `last_error`.
- [ ] **C2.** `EXISTS` — `submit_*` raise `ConflictError` on terminal entities.
  - `Test:` submitting an already-`approved` brand raises `ConflictError` and does not
    mutate status.
- [ ] **C3.** `EXISTS` — `/status` routes require a platform operator.
  - `Verify:` `require_platform_operator` in `app/auth/deps.py` (unverified here).
  - `Test:` a non-operator caller of `/status` is refused and the status is unchanged.
- [ ] **C4.** `EXISTS (observed)` — `/status` routes call `advance_status` but **discard its
  boolean return**, then commit unconditionally. An ignored transition therefore still
  commits (writes nothing new) and returns 200. Confirm this is intended; it means the
  API reports success for a no-op.

### D. Numbers on a campaign

- [ ] **D1.** `EXISTS` (local): `numbers_on_campaign` counts `OrgNumber.campaign_id`.
  It is a **count only**; there is no attach/detach in the audited sources.
- [ ] **D2.** `GATE`: numbers must not be attachable to a campaign that is not
  carrier-approved. `NOT BUILT (audited)` — no such guard is shown. The 10DLC module
  docstring says numbers without an approved campaign may not send, but the enforcement
  point is not in the audited files.
  - `Verify:` the code that assigns `OrgNumber.campaign_id`.
  - `Test:` assigning a number to a non-approved campaign is refused.

### E. Billable operations and spend control

- [ ] **E1.** 10DLC brand/campaign registration is a **billable** carrier action and a
  rejection can forfeit the fee. `OWNER DECISION`: budget plus authorization to file
  production applications. **There is no in-repo source for the fee schedule; confirm
  amounts and refundability from the carrier's current published terms before asserting
  them anywhere.**
- [ ] **E2.** Prepaid / settlement policy. `OWNER DECISION`.
  - Decide whether balances must be checked before any billable carrier call, and where
    that check lives. `NOT BUILT (audited)`.
  - `Test:` (once the policy exists) a submit with insufficient balance is refused
    **before** the carrier call, not after.

### F. KYC / entity identity

- [ ] **F1.** `EXISTS (local)`: EIN required unless `SOLE_PROPRIETOR`.
- [ ] **F2.** KYC enablement. `OWNER DECISION` — whether KYC is required for the target
  entity types and where it is enforced. `NOT BUILT (audited)`.

### G. Bring-your-own-number (BYON)

- [ ] **G1.** `NOT BUILT (audited)`; nothing in the audited files references a carrier
  number port or external number ownership.
- [ ] **G2.** `OWNER DECISION` — supported or not; if supported, define ownership checks
  that run before any campaign attach.

### H. Credentials, profiles, and configuration

- [ ] **H1.** `NOT BUILT (audited)` — no carrier client or config in the audited files.
- [ ] **H2.** `OWNER DECISION` — production API credentials, messaging profile(s), and the
  secret used to authenticate any automated approval source, provisioned and rotated
  outside the repository. **Do not** record any credential or host in this document.

### I. Deploy

- [ ] **I1.** `OWNER DECISION` — deploy window and a rollback plan. A migration is
  required **only if** a later read shows `carrier_refs` is insufficient (section 2.3);
  do not assume one. `NOT BUILT (audited)`.

## 5. Required sequence and safety gates

Ship in this order. Each gate is a stop condition: **do not** let a later step treat an
earlier step's local state as carrier truth.

1. **G0 — Decide the approval source.** The current path is operator `/status` (EXISTS).
   Decide whether to keep manual-only, add a verified webhook, or add a verified poll
   (B2). If a human keeps approving, document who and what evidence they check.
2. **G1 — Populate and read `carrier_refs` (A1).** No schema change assumed; verify
   `carrier_refs` is enough before any migration is proposed. Ensure `TfvOut` surfaces it
   (section 2.3) if TFV refs must be visible.
3. **G2 — Real carrier submit (A2, A3, H1).** Outbound create with idempotency; write
   `"submitted"` **only** on carrier acceptance; store `carrier_refs["telnyx"]` in the
   same transaction. A failed call must leave the entity submittable, not `"submitted"`.
4. **G3 — Verified automated source (B2–B5), if adopted.** Authenticate, map by
   `carrier_refs`, route through `advance_status`. Unauthenticated or unmappable input
   means log plus no-op.
5. **G4 — Lock the state machine (C).** Add the tests in section 6; no direct `status`
   assignment anywhere outside `advance_status`, including new automation.
6. **G5 — Gate number attachment (D2)** on a carrier-approved campaign only.
7. **G6 — Spend controls (E2)** before billable calls.
8. **G7 — KYC / BYON decisions (F, G)** enforced before submit where required.
9. **G8 — Deploy (I1)** with a rollback plan (and a migration only if proven necessary).

**Invariant that must hold at every step:**

> `status ∈ {"approved", "rejected"}` **only if** a trusted writer (an authorized
> platform operator today, per section 2.2, or a future verified source) advanced it
> there. A local `submit_*` **never** yields `"approved"`, and a local `"submitted"` is
> never treated as carrier-submitted.

## 6. Acceptance test matrix

Names are suggestions; place them beside the existing tests that import
`app.services.registration` (confirm the test package path — it is not assumed here).

| # | Test | Setup | Assertion |
| - | ---- | ----- | --------- |
| T1 | `unknown_status_rejected` | any entity | `advance_status(e, "bogus")` raises `ValidationFailedError` |
| T2 | `none_status_is_draft` | unflushed entity | advancing to `"submitted"` succeeds from `None` |
| T3 | `late_submitted_does_not_demote_approved` | status `approved` | advancing to `"submitted"` leaves status `approved`, returns `False` |
| T4 | `terminal_status_is_sticky` | status `approved` | advancing to `"rejected"` is ignored and `last_error` is unchanged |
| T5 | `rejected_sets_last_error` | status `submitted` | advancing to `"rejected"` with an error stores that error in `last_error` |
| T5b | `rejected_is_terminal` | status `rejected` with error | advancing to `"approved"` is ignored; status and `last_error` unchanged |
| T6 | `campaign_requires_approved_brand` | brand `submitted` | `submit_campaign` raises `ValidationFailedError` |
| T7 | `submit_terminal_conflicts` | brand `approved` | `submit_brand` raises `ConflictError`, status unchanged |
| T8 | `status_route_requires_operator` | non-operator caller | `/status` is refused and status is unchanged |
| T9 | `failed_carrier_call_is_not_submitted` | simulated carrier error | entity is **not** `"submitted"` and no `carrier_refs["telnyx"]` is set |
| T10 | `resubmit_is_idempotent` | existing `carrier_refs["telnyx"]` | one carrier create, stable ref |
| T11 | `unverified_source_is_rejected` | bad/missing signature (or missing poll auth) | no status change |
| T12 | `unknown_reference_noop` | unmatched `carrier_refs` value | no status change, no crash |
| T13 | `approval_replay_is_noop` | same `approved` transition twice | the second is a no-op |
| T14 | `number_attach_requires_approved_campaign` | campaign `submitted` | attach is refused |

Tests backed by `EXISTS` code: **T1–T8**. Tests covering behavior **not built** in the
audited sources: **T9–T14** (targets, not currently passing). The removed earlier draft
claimed `rejected` could later be cleared; that is impossible because `rejected` is in
`TERMINAL_REGISTRATION`, so T5/T5b replace it with the correct, terminal-aware assertions.

## 7. Owner decisions and prerequisites (blockers)

- Authorization to file **billable** 10DLC brand/campaign applications, plus a
  rejection-cost budget. Confirm the fee schedule from an authoritative carrier source;
  do not assert amounts here.
- Production carrier **credentials** and messaging profile(s), provisioned outside the
  repository.
- Secret/mechanism for authenticating any future automated approval source (webhook or
  poll).
- **KYC** enablement scope.
- **Prepaid** / settlement policy and where it is enforced.
- **Pricing** pass-through (if any) for billable registrations.
- **BYON** support decision.
- **Deploy** window and a rollback owner (migration only if proven necessary).

## 8. Explicit non-goals for the implementer

- No live carrier calls, deployments, or git operations as part of this document.
- No credentials, hostnames, account IDs, or secrets recorded here.
- Do not mark any item **EXISTS** without citing a symbol in a source file.

## 9. Open questions to resolve before coding

1. Is manual operator approval (the `/status` routes) sufficient, or is a verified
   webhook/poll required, and who is accountable for the evidence behind an approval?
2. Is `carrier_refs` sufficient to store a Telnyx reference for all three entity types,
   including on `TollFreeVerification` where it is stored but not exposed by `TfvOut`?
3. What assigns `OrgNumber.campaign_id`, and is it gated on carrier approval?
4. Is a failed submission rolled back at the route level? (A4 assumes the existing error
   handlers map domain errors, but that was not verified here.)
