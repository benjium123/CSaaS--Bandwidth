# Runbook — Telnyx Toll-Free Verification (TFV) Filing Reconciliation

> **Status:** Active operator runbook (documentation only). Writing this document changes
> nothing: it does not retry, repair, or file anything by itself.
>
> **Documentation only.** No source code, tests, deployment state, or git history is changed
> by this runbook, and no live carrier call is authorized by it.
>
> **Placeholders.** `{tfv_id}`, `{attempt_id}`, `{request_id}`, `{number_e164}`, and
> `{business_name}` are placeholders. Never paste the real values of a phone number, an
> account identifier, business/contact data, or any secret into this document, a shared
> ticket, or a chat channel. See §9.

---

## 1. Purpose

This runbook tells a Platform Operator how to handle an **ambiguous Telnyx toll-free
verification (TFV) filing** — a filing whose outcome is unknown because the original carrier
`POST` timed out, was interrupted, or otherwise did not return a usable result.

The filing service durably records "attempted, outcome unknown" **before** it calls the
carrier, so the ambiguous case is a real, expected state and not a bug. This runbook governs
how that state is resolved safely:

1. by the guarded, **read-only** reconcile route when the carrier evidence is unambiguous; and
2. by a **tightly controlled** marker-repair procedure when it is not.

The one rule that overrides everything else: **never blindly retry the filing POST** (§6).

---

## 2. Scope

**In scope**

- The Telnyx TFV filing service `file_tollfree_verification_with_telnyx`
  (`backend/app/services/telnyx_tollfree_filing.py`) and the guarded operator reconcile route
  `POST /api/v1/registration/tollfree/{tfv_id}/reconcile-telnyx`
  (`backend/app/api/routes/registration.py`).
- The Telnyx TFV transport `TelnyxTollfreeVerificationClient` (`create`, `get`,
  `list_requests`) in `backend/app/providers/telnyx/tollfree_verification.py`.
- The durable `carrier_refs` marker and the `carrier_refs["telnyx"]` reference.
- Reading a record's state through `GET /api/v1/registration/tollfree` (`TfvOut.carrier_refs`).

**Out of scope**

- 10DLC brand and campaign filing, carrier number association, and the send gate.
- Any code change, new endpoint, schema migration, or deployment action.
- Any live or billable carrier test (§14).

---

## 3. Prerequisites

Before starting, confirm all of the following. If any is missing, stop and escalate.

- **Authorization.** The acting human holds the platform-operator (super admin) role **and**
  the `compliance:manage` permission. Both are required by the reconcile route dependency
  chain (`require_platform_operator` **and** `require_permission("compliance:manage")`).
- **Tooling.** `curl`/HTTP client access to the registration API with that operator's
  credentials, and API read access to `GET /api/v1/registration/tollfree`.
- **Known record.** The exact `{tfv_id}` of the affected `TollFreeVerification`, and the
  ability to read its `status` and `carrier_refs` (via the list endpoint).
- **Carrier configuration.** An active Telnyx provider account (or a globally configured key)
  resolved by `_resolve_telnyx_settings`. A missing/blank key surfaces as a
  `ValidationFailedError` and must be fixed before reconciliation can run.
- **Log access.** Permission to read the structured application logs (event names only — see
  §9 for what may be copied out).
- **Audit channel.** A ticketing channel and an audit trail where the incident template (§12)
  will be recorded.

---

## 4. Roles and authorization boundaries

Role names are used deliberately; no individual's name belongs in a ticket or in this file.

| Role | Responsibility |
| --- | --- |
| **Platform Operator** | Holds the platform-operator (super admin) role **and** `compliance:manage`. Runs the guarded reconcile route, reads records, proposes a marker repair, and executes the reviewed change. |
| **Incident Commander** | Owns the incident end to end, decides when to escalate to Telnyx support, convenes the two-person review for a marker repair, and signs off the audit trail. |
| **Change Reviewer / Second Approver** | Independent second person (a different Platform Operator or a change-management approver). Verifies carrier evidence and the exact before/after state. Required for any marker repair (§11). |
| **Telnyx Support** | External carrier support; the only party that can authoritatively state whether the filing POST created a request when the reconcile route returns zero records. |

**Authorization boundaries (enforced by the code, not by this runbook):**

- The reconcile route is reachable **only** by a platform operator that also holds
  `compliance:manage`.
- The reconcile route requires **no** `confirm_non_refundable` acknowledgement, because it
  cannot create or spend — it is read-only against the carrier.
- The filing route `POST /tollfree/{tfv_id}/file-telnyx` separately requires the same
  operator + permission **and** `confirm_non_refundable=true`; it is billable (§14).
- This runbook grants no data-edit rights. It describes safeguards around a change that, if it
  is ever needed, goes through the platform's normal change-management process (§11).

---

## 5. Durable markers and `carrier_refs["telnyx"]`

A `TollFreeVerification` may hold two Telnyx entries inside its `carrier_refs` JSON column
(the column is shared with other carriers):

| `carrier_refs` key | Type | Meaning |
| --- | --- | --- |
| `"telnyx"` | string | The **carrier request id** (`{request_id}`). Written **only** after a confirmed filing success or a confirmed reconciliation adoption. Its presence means the filing is **not ambiguous**. |
| `"telnyx_tfv_filing"` | dict | The **durable pending-attempt marker**. Its presence means "an attempt was made; the outcome may be unknown". |

The marker dict has exactly these fields:

| Marker field | Meaning |
| --- | --- |
| `status` | Must be `"pending"` for reconciliation to proceed. Any other value refuses reconciliation. |
| `attempt_id` | The service-generated UUID identifying this attempt. Non-empty string required. |
| `attempted_at` | The instant the attempt was made, as a **timezone-aware** ISO-8601 datetime. It is used verbatim as the carrier list filter `date_start`. Non-empty and timezone-aware are both required. |

Key facts an operator must hold in mind:

- The marker is written and **committed before** the carrier `POST`. That is what makes a
  retry after a timeout safe to refuse: the next caller sees the marker.
- On **any** error the filing service leaves the existing local `status` and the marker
  **exactly as they were**. It never clears the marker as part of a retry.
- On a confirmed success (2xx with an id) the service writes `carrier_refs["telnyx"]`,
  removes the marker, and advances the local status to `submitted` — all in one commit.
- The marker is readable without database access: `GET /api/v1/registration/tollfree` returns
  `TfvOut.carrier_refs`.

---

## 6. Safety rule — never blindly retry the filing POST

> **On any ambiguous filing attempt, do NOT call `POST /tollfree/{tfv_id}/file-telnyx` again.**

The filing service will normally refuse the repeat by itself, because the durable marker (or
an existing `carrier_refs["telnyx"]`) is present. That guard is a backstop, **not** a licence
to try. The safety rule is absolute because:

- Telnyx may already have **accepted** the original `POST`; a timeout on our side does not
  mean the carrier rejected it. A second `POST` can create a **duplicate, billable, and
  non-refundable** registration.
- If the marker were ever missing (for example after an out-of-band change), the service's own
  guard would not stop a re-POST — the operator must.

Resolve the ambiguity first (§7/§8), or escalate (§10). Never paper over it with a retry.

It is equally important to distinguish the two operations:

- **Filing** (`/file-telnyx`) is a billable `POST`. Never retry it blindly.
- **Reconciliation** (`/reconcile-telnyx`) is a read-only `GET`/list. Retrying it is safe
  (§7).

---

## 7. Normal use of the guarded reconcile route

```
POST /api/v1/registration/tollfree/{tfv_id}/reconcile-telnyx
```

**Authorization:** platform operator **and** `compliance:manage`. No
`confirm_non_refundable` is required — this route cannot create or spend.

**What it does:** exactly **one** read-only carrier call — the filtered verification **list**
(`TelnyxTollfreeVerificationClient.list_requests`) with the exact filing filters:

- `page=1`, `page_size=100`;
- `phone_number` = the number's E.164;
- `business_name` = the verification's business name;
- `date_start` = the marker's `attempted_at`.

**What it never does:** never `POST`, never `create`, never retry the filing, never approve or
reject locally, and never set `verificationStatus`.

**Preconditions it enforces (all fail closed):**

1. The record is **undecided** — `approved`/`rejected` are refused before any carrier read
   (`_require_reconcilable_status`).
2. The number is usable: same org, an actual toll-free number, a Telnyx number, still active
   (`_require_usable_number`).
3. A pending `telnyx_tfv_filing` marker exists with a non-empty `attempt_id` and
   `attempted_at`, and **no** `carrier_refs["telnyx"]` is recorded (`_reconcilable_attempt`).
4. `attempted_at` is a timezone-aware ISO datetime (`_parse_attempted_at`), validated **before**
   the carrier call.

**What it does on success:** adopts the carrier request id **only** when the page is
unambiguous — exactly one record (`total_records == 1` and `records` has length 1), carrying
an `id`, whose `businessName` matches this verification and whose single `phoneNumbers` entry
matches this exact number (`_match_reconciled_request`). On adoption it writes
`carrier_refs["telnyx"]`, removes the pending marker, and advances the local status to
`submitted` (monotonic). It **never** sets `approved` or `rejected` locally.

**What it does on anything else:** refuses (`ConflictError`/`ValidationFailedError`/
`FeatureUnavailableError`) and **leaves the pending marker untouched**.

**Good practice:** a plain lookup of a single carrier request is **not** reconciliation. Only
this explicit, exact-match list path reconciles. Run the route, then read the record back with
`GET /api/v1/registration/tollfree` and confirm `carrier_refs["telnyx"]` and `status`.

---

## 8. Decision trees

For every case, first read the record state with `GET /api/v1/registration/tollfree` and
confirm exactly which of these is true: `status`, presence of `carrier_refs["telnyx"]`,
presence and shape of the `telnyx_tfv_filing` marker.

### Case A — Zero matches

- **What you see:** the reconcile route returns a conflict; the carrier list returned no
  records (or `total_records` is 0) at the exact filters.
- **What the code does:** `_match_reconciled_request` refuses with
  "…did not return exactly one matching…". The marker is left in place.
- **What you do:**
  1. Confirm the pending marker and the absence of a `telnyx` ref.
  2. It is safe to run the reconcile route again (read-only, no spend), but do so only a small,
     **bounded** number of times — a zero result at the exact filters is stable.
  3. Escalate to Telnyx support (§10) to establish whether the original `POST` ever created a
     request.
  4. Only if Telnyx support confirms **in writing** that no request exists may a controlled
     marker repair (§11) clear the pending marker, so a fresh, deliberately reviewed filing
     decision can be made.
- **What you must not do:** re-POST the filing; hand-write a `carrier_refs["telnyx"]` value.

### Case B — Multiple matches

- **What you see:** the reconcile route returns a conflict; the list returned more than one
  record (`total_records != 1`).
- **What the code does:** same refusal as Case A. Marker untouched.
- **What you do:** escalate. A human must **not** choose one of several records.
- **What you must not do:** adopt one record by hand; re-POST.

### Case C — Mismatched record

- **What you see:** the reconcile route returns a conflict naming a different business or a
  different number.
- **What the code does:** exactly one record was returned, but its `businessName` or its
  `phoneNumbers[0].phoneNumber` did not equal this verification's values, so it is refused.
- **What you do:** escalate. The single record belongs to a different business/number, so
  adopting it would misattribute carrier state.
- **What you must not do:** overwrite the ref with that id; treat the near-match as a match.

### Case D — Carrier timeout or transport / HTTP error

- **What you see:** the reconcile route surfaces a carrier error. A timeout or unreachable
  transport is a `FeatureUnavailableError`; an HTTP `>= 400` is classified (retryable →
  `FeatureUnavailableError`, otherwise `ValidationFailedError`).
- **What the code does:** the service logs only the error type and re-raises; the pending
  marker is untouched.
- **What you do:** retrying the reconcile route is **safe** (read-only, never spends). Retry
  with **bounded retries and operational backoff** — no specific retry count or timing is
  mandated by this runbook or implemented in the code — and escalate (§10) if it persists. A
  missing/blank API key also surfaces here as a `ValidationFailedError` — check carrier
  account/key provisioning.
- **What you must not do:** treat a reconcile failure as permission to re-POST the filing.

### Case E — Malformed marker

- **What you see:** the reconcile route returns a conflict ("marker is incomplete") or a
  validation failure ("non-ISO attempted_at" / "not timezone-aware").
- **What the code does:** `_reconcilable_attempt` refuses a marker that is not a non-empty
  dict, whose `status` is not `"pending"`, or that lacks a non-empty `attempt_id`/`attempted_at`;
  `_parse_attempted_at` refuses a non-ISO or non-timezone-aware `attempted_at` **before** any
  carrier call. The marker is never mutated.
- **What you do:** record the marker's **shape** (which fields are present/absent, and the
  `attempt_id`) and escalate. Recovery requires the controlled marker-repair procedure (§11).
- **What you must not do:** overwrite the marker ad hoc; guess an `attempted_at`.

### Case F — Marker without a request id

- **What you see:** a pending `telnyx_tfv_filing` marker and **no** `carrier_refs["telnyx"]`.
- **What the code does:** this is exactly the reconcilable ambiguous state.
- **What you do:** run the guarded reconcile route (§7). This is the normal path.
- **What you must not do:** re-POST.

### Case G — Request id without a marker

- **What you see:** `carrier_refs["telnyx"]` is present; there is no marker (or it is gone).
- **What the code does:** `_reconcilable_attempt` refuses — "a Telnyx request id is already
  recorded… the filing is not ambiguous."
- **What you do:** nothing to reconcile. The filing succeeded. If the record is still **not yet
  decided** (for example `draft`/`submitted`) and you need to record an initial carrier
  decision, `POST /tollfree/{tfv_id}/status` with `approved` is **fail-closed**: it does a
  fresh Telnyx `GET`, requires the returned id to equal the stored ref, and requires
  `verificationStatus == "verified"`. Otherwise wait for the carrier. (This is a first-time
  decision on an undecided record, not a refresh of an already-decided one — see Case H.)
- **What you must not do:** run reconcile to "refresh" — it refuses by design.

### Case H — Already-resolved records

- **What you see:** `status` is `approved` or `rejected`.
- **What the code does:** `_require_reconcilable_status` refuses **before any carrier read** —
  a decided record must not be touched by a read-driven repair.
- **What you do:** nothing to reconcile and nothing to refresh. There is currently **no**
  status rewrite or reopen path: terminal status is sticky (`TERMINAL_REGISTRATION`). Do **not**
  call `POST /tollfree/{tfv_id}/status` merely to refresh an already-approved terminal record —
  the carrier-confirmed `approved` guard runs first, but an `approved` decision on a record that
  is already `approved` changes nothing, so `_apply_status_or_conflict` rolls the transaction
  back and raises a `ConflictError` (a deliberate no-op conflict, not a refresh). Leave the
  record as it is and let the carrier decide.
- **What you must not do:** attempt to demote or re-open a terminal status; call `/status` to
  "refresh" a decided record; re-POST.

---

## 9. Evidence to collect (and never copy)

Collect enough to prove what happened **without** copying PII or secrets anywhere.

**Safe to record in the ticket / audit trail:**

- the internal `{tfv_id}` (and, if useful, the internal `{attempt_id}`);
- the local `status` before and after;
- **presence/absence** of each marker field (a boolean per field), not business/contact data;
- `attempted_at` as a **UTC** timestamp;
- the reconcile route's result **category** (e.g. "zero records", "more than one record",
  "one record, number did not match", "timeout", "HTTP error category");
- the transport/HTTP **error type or status class** (for example "timeout", "HTTP 4xx");
- the Telnyx **request id** `{request_id}` once it exists (for the carrier's own case).

**Never record in a ticket, chat, log paste, or this document:**

- the API key / bearer token or any secret;
- the phone number (`{number_e164}`) or business name / contact data / EIN;
- account identifiers or messaging-profile identifiers;
- raw carrier request/response bodies (the transport deliberately never logs them).

Describe number/business mismatches by **category** ("the returned record's number did not
match") rather than by writing the actual values.

---

## 10. Escalation to Telnyx support

**Escalate when:** the reconcile route returns zero records (Case A), multiple records
(Case B), a mismatched record (Case C), a persistent carrier timeout/error (Case D), or when a
malformed marker (Case E) needs the controlled repair in §11.

**Provide to Telnyx support (non-sensitive identifiers only):**

- the Telnyx **request id** `{request_id}` **when one is recorded** — the carrier already holds
  it and can look it up directly;
- the **UTC** `attempted_at` timestamp of the filing attempt;
- the internal `{attempt_id}` for cross-reference;
- a plain description of the failure category (for example "one attempt, no request id
  returned, no record found on list").

**Do not provide:** the API key, raw request/response bodies, or any unrelated record. If the
carrier's secure support channel requires verification inputs (such as the number or business
name) to locate a request, supply them only through that channel, never in a local or shared
ticket.

Record the support case reference (and the outcome) in the audit trail (§12).

---

## 11. Controlled marker-repair procedure

> **Read this first.** There is **no** API, route, or service function that repairs a durable
> marker, and the reconcile route **never** clears a marker on failure. Do **not** invent or
> add a repair endpoint "to make it easy", and do **not** edit production data ad hoc.
> A repair — if it is genuinely required — is a controlled **data change** that goes through
> the platform's normal change-management process. This runbook authorizes the *process and
> safeguards*, not a tool.

### 11.1 When a repair is permitted

Only two situations justify touching a durable marker, and the correct default in every other
case is to **escalate, not repair**:

1. **Clear a pending marker after carrier-confirmed non-creation.** Telnyx support has stated
   in writing that the original filing `POST` created **no** verification request (the zero
   match of Case A). The marker records an attempt that demonstrably did not become a carrier
   request, so it may be removed — enabling a fresh, deliberately reviewed filing decision.
2. **Normalize a malformed/incomplete marker** into the shape the service requires
   (`status="pending"`, non-empty `attempt_id`, timezone-aware `attempted_at`), using the
   attempt id and attempt time already recorded in logs/audit — **only** so the record becomes
   reconcilable. This must not claim any outcome.

A multiple-match or a mismatched record is **never** repaired by hand — those escalate.

### 11.2 Two-person approval

- A **Platform Operator** proposes the repair and prepares the exact before/after state.
- An **independent second approver** (Change Reviewer / a different Platform Operator)
  verifies the carrier evidence, the exact record, the exact field changes, and the rollback.
- One person may **not** both propose and approve. The Incident Commander records both.

### 11.3 Backup / snapshot

- Take a targeted snapshot of the affected `TollFreeVerification` row (or the platform's
  standard pre-change database backup) **before** any change.
- Record the snapshot reference in the audit trail. The snapshot is the rollback source.

### 11.4 Exact carrier evidence required

- **For a clear (11.1.1):** a written Telnyx support statement that **no** verification request
  exists for this filing, plus a local read confirming no `carrier_refs["telnyx"]` is recorded.
- **For a normalize (11.1.2):** the recorded `attempt_id` and `attempted_at` (UTC) from
  logs/audit, plus a local read confirming no `carrier_refs["telnyx"]` is recorded.
- Record evidence **by reference** (support case id, ticket id). Do not paste PII or secrets.

### 11.5 Exact change envelope

- Change **only** the minimum `carrier_refs["telnyx_tfv_filing"]` content needed, or remove
  that marker key when 11.1.1 applies. Nothing else.
- **Never** change `status` in the same operation. **Never** write, replace, or invent a
  `carrier_refs["telnyx"]` value — that is fabrication (§14).

### 11.6 Audit log fields (record all)

- Incident / ticket id.
- Date-time (UTC).
- Acting Platform Operator (role; identity per local policy).
- Second approver (role; identity per local policy).
- `{tfv_id}`.
- Marker **before**: which fields were present, the `attempt_id`, the `attempted_at`.
- `carrier_refs["telnyx"]` before: present / absent.
- Local `status` before.
- Carrier evidence relied on (support case reference; the category of outcome).
- Reconcile attempts made (count and result category for each).
- Action taken (clear / normalize) and the exact resulting marker (which fields present, values).
- Snapshot / backup reference.
- Post-repair verification result (§11.8).
- Rollback reference (§13).
- Reviewed-by / approved-by.

### 11.7 Rollback

- If the repair proved wrong or the carrier evidence is withdrawn, restore the affected row
  from the pre-change snapshot. See §13.

### 11.8 Post-repair verification

- Re-read the row with `GET /api/v1/registration/tollfree` and confirm the marker matches the
  intended change and that `status` is unchanged.
- If the repair cleared the marker (11.1.1) and a fresh filing is intended, that filing is a
  **separate, deliberate, billable** action (§14) and requires its own authorization.
- If the repair normalized the marker (11.1.2), run the guarded reconcile route (§7) and
  confirm the outcome category.
- Close the audit trail only after the post-repair state is recorded.

---

## 12. Incident / audit template

```
Incident / ticket id:
Date-time (UTC):
Incident Commander:
Acting Platform Operator (role):
Second approver (role):
TFV record (internal id):
Initial state:
  status:                 <draft | submitted | approved | rejected>
  carrier_refs["telnyx"]:   <present | absent>
  marker present:          <yes | no>
  marker fields present:   status? <y/n>  attempt_id? <y/n>  attempted_at? <y/n>
  attempted_at (UTC):      <value if present>
Observation:
  reconcile result category: <zero | multiple | one-mismatch | timeout | HTTP error | adopted>
  carrier error category:    <e.g. timeout / HTTP 4xx / none>
Carrier evidence relied on:
  Telnyx support case ref:
  Outcome stated by carrier:  <no request exists | request id {request_id} | other>
Action taken:
  <none | ran reconcile route | controlled marker repair (clear | normalize)>
  Exact before/after:
Change-management reference:
Snapshot / backup reference:
Rollback plan:
Post verification summary:
Reviewed-by / approved-by:
```

*(No phone numbers, business/contact data, account identifiers, or secrets in this template.)*

---

## 13. Rollback steps

Rollback restores the record to its pre-action state and re-opens nothing by itself.

1. **If the reconcile route adopted a ref** and that adoption is later shown to be wrong
   (this should not happen — adoption requires an exact match): restore the affected row's
   `carrier_refs` and `status` from the pre-change snapshot, then escalate to the Incident
   Commander. Do not hand-edit several fields to "fix" it.
2. **If a controlled marker repair was performed** and is judged wrong: restore the affected
   row from the pre-repair snapshot (§11.3).
3. **If a marker was cleared and a fresh filing has NOT yet been made:** restoring the
   snapshot returns the record to "attempted, outcome unknown" and the reconcile route will
   treat it as ambiguous again — the correct starting point for a new review.
4. **If a fresh filing has already been made** after a clear, do **not** roll back by editing
   the record; escalate immediately. A carrier request may now exist, and §6 applies.
5. Record the rollback in the audit trail (§12) with the same rigor as the original change.

---

## 14. Prohibitions

These are hard prohibitions. Violating any of them can create a duplicate billable
registration, misattribute carrier state, or trust fabricated evidence.

- **Never blindly retry the filing `POST`** after an ambiguous attempt (§6).
- **Never change terminal status.** `approved` and `rejected` are sticky
  (`TERMINAL_REGISTRATION`); do not demote, re-open, or reassign them, and there is currently
  no status rewrite/reopen path — do not call `/status` merely to refresh a decided record.
- **Never fabricate a carrier ref.** Do not write, guess, copy, or "borrow" a
  `carrier_refs["telnyx"]` value. A carrier ref may only be written by the filing service or
  by the reconcile route from a single exact-match carrier record.
- **Never make casual or ad-hoc database edits.** Marker repair, if required, happens only
  through the two-person, backed-up, evidence-based, audited procedure in §11 — never as a
  quick fix.
- **Never test with live or billable carrier calls.** The reconcile route is read-only, but
  `/file-telnyx` is billable and non-refundable; do not use it to "see what happens".
- **Never rely on a plain carrier lookup** as reconciliation — only the exact-match list path
  reconciles.
- **Never approve locally.** Filing and reconciliation only ever advance the local status to
  `submitted`; approval is decided by the carrier.

---

## 15. Verification checklist

Use before closing an incident.

- [ ] Authorization confirmed: platform operator **and** `compliance:manage` (and, for a
      repair, an independent second approver).
- [ ] Record state read via `GET /api/v1/registration/tollfree` (no database access needed):
      `status`, `carrier_refs["telnyx"]`, marker presence/shape.
- [ ] Case identified against §8 and the matching action taken.
- [ ] No filing `POST` was retried after the ambiguous attempt.
- [ ] The reconcile route (`POST /tollfree/{tfv_id}/reconcile-telnyx`) was used for
      reconciliation, and it performed read-only list calls only.
- [ ] On a successful adoption: `carrier_refs["telnyx"]` is recorded, the marker is gone, and
      `status` is `submitted` (never `approved`/`rejected`).
- [ ] On any refusal: the pending marker is still present and untouched.
- [ ] For an already-decided record: no `/status` call or other refresh was attempted, and the
      terminal status was left as-is.
- [ ] Any marker repair followed §11 in full (two-person approval, snapshot, exact carrier
      evidence, §11.6 audit fields, rollback plan, post-repair verification).
- [ ] No phone number, business/contact data, account identifier, or secret was copied into a
      ticket or this document.
- [ ] Escalation and its outcome (if any) are recorded (§10).
- [ ] Terminal status was neither changed nor re-opened.
- [ ] No live/billable call was made.

---

## 16. Repository references

Cite by symbol; the source of truth is the code.

| Path | Symbol / route | Relevance |
| --- | --- | --- |
| `backend/app/services/telnyx_tollfree_filing.py` | `file_tollfree_verification_with_telnyx` (`file_tfv_with_telnyx`) | Billable filing; writes the pre-POST marker; never approves locally. |
| `backend/app/services/telnyx_tollfree_filing.py` | `reconcile_tollfree_filing_with_telnyx` | Read-only exact-match reconciliation; never POSTs, never approves. |
| `backend/app/services/telnyx_tollfree_filing.py` | `_reconcilable_attempt`, `_parse_attempted_at`, `_match_reconciled_request`, `_require_reconcilable_status`, `_require_usable_number` | The fail-closed validation and match rules in §7/§8. |
| `backend/app/services/telnyx_tollfree_filing.py` | `_REQUEST_ID_KEY = "telnyx"`, `_ATTEMPT_KEY = "telnyx_tfv_filing"` | The two `carrier_refs` keys in §5. |
| `backend/app/api/routes/registration.py` | `reconcile_tfv_telnyx` — `POST /api/v1/registration/tollfree/{tfv_id}/reconcile-telnyx` | The guarded reconcile route in §7. |
| `backend/app/api/routes/registration.py` | `file_tfv_telnyx` — `POST /api/v1/registration/tollfree/{tfv_id}/file-telnyx` | The billable filing route referenced by §6/§14. |
| `backend/app/api/routes/registration.py` | `set_tfv_status` — `POST /api/v1/registration/tollfree/{tfv_id}/status` | Carrier-confirmed decision route for a not-yet-decided record (Case G); not a refresh path for an already-decided record (Case H). |
| `backend/app/api/routes/registration.py` | `_require_tfv_approved`, `_tfv_is_approved`, `_apply_status_or_conflict` | The fail-closed `approved` guard (`verificationStatus == "verified"`) and the no-op-is-a-conflict behaviour behind Case H. |
| `backend/app/api/routes/registration.py` | `list_tfv` — `GET /api/v1/registration/tollfree`, `TfvOut.carrier_refs` | Reading record state without database access. |
| `backend/app/providers/telnyx/tollfree_verification.py` | `TelnyxTollfreeVerificationClient.create/get/list_requests`, `_validated_list` | The transport; `list_requests` is the single read used by reconciliation. |
| `backend/app/auth/deps.py` | `require_platform_operator`, `require_permission` | The authorization boundary in §4. |
| `backend/app/models/numbers.py` | `TERMINAL_REGISTRATION`, `TollFreeVerification` | Terminal stickiness and the record. |
| `docs/LAUNCH_READINESS.md`, `docs/TELNYX_IMPLEMENTATION_GAPS.md` | — | Launch status and the open P0 items this runbook addresses. |
