# Launch Readiness — Telnyx 10DLC / TFV Registration

> **Status: NOT launch-ready.** Corrects `docs/TELNYX_IMPLEMENTATION_GAPS.md` (predates the
> filing code) and supersedes earlier drafts; the original is left unedited. Claims cite
> symbols in the audited files only. No secrets; no live carrier calls, and no live-carrier
> integration test yet — production Telnyx credentials remain an external prerequisite (§6).

## 0. Sources inspected

`api/routes/registration.py`, `api/routes/numbers.py`, `compliance/registration.py`,
`services/telnyx_brand_filing.py`, `services/telnyx_campaign_filing.py`,
`services/telnyx_number_association.py`, `providers/telnyx/registration.py`,
`providers/telnyx/tollfree_verification.py`, `providers/telnyx/tollfree_payload.py`, and the
offline tests in §8. Referenced but not supplied, therefore **UNVERIFIED**:
`provider_accounts`, `providers/telnyx/registration_status` (`map_brand_status`,
`map_campaign_status`, `APPROVED`), `brand_payload`/`campaign_payload`, `config.Settings`,
`models.numbers`, `auth.deps`.

## 1. Corrections to the gaps doc and earlier drafts

- A1/A2 ("store the Telnyx ref", "idempotency") were **NOT BUILT**; **now built** — filing
  writes `carrier_refs["telnyx"]` on success and commits a durable attempt marker first.
- Approval was operator-asserted; it is **now fail-closed** — an `approved` decision is
  accepted only after a fresh Telnyx GET confirms the matching record approved.
- D2 (no attachment guard) is **built** for Telnyx: the PATCH path uses a carrier association
  service, the order path refuses a Telnyx `campaign_id` before spend, and the send gate is fail-closed.
- Still true: `TfvOut` hides `carrier_refs`; TFV has no filing route (§3).
- Stale docstrings: `telnyx_number_association.py` ("NOT ROUTED") and
  `telnyx_campaign_filing.py` are both reached from `registration.py`/`numbers.py`.

## 2. Actually implemented (EXISTS)

**Carrier-confirmed approvals — `api/routes/registration.py`.** `set_brand_status`,
`set_campaign_status`, `set_tfv_status` accept a `StatusIn`; `_is_approval` routes an
`approved` decision through `_require_brand_approved`/`_require_campaign_approved`/
`_require_tfv_approved`, each of which demands a stored `carrier_refs["telnyx"]`, does a
fresh Telnyx GET, refuses on any transport/HTTP error, requires the returned id to equal the
ref, and requires the mapped status approved (`map_brand_status`/`map_campaign_status` ==
`APPROVED`; TFV `verificationStatus == "verified"`). Tests inject
`app.state.telnyx_http_client`.

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

**Transports / payload.** `TelnyxRegistrationClient`, `TelnyxTollfreeVerificationClient`,
the pure `build_tollfree_verification_payload`, single attempt, no auto-retry, no secrets logged.

## 3. NOT implemented / remaining

- **TFV filing service/route.** The payload builder and transport exist, but nothing files a
  TFV at Telnyx or writes its `carrier_refs["telnyx"]`; there is no TFV attempt marker.
- **Stale approval trusted on send.** The gate trusts persisted local evidence; `approved` is
  terminal, so a later carrier downgrade cannot be reflected.
- **No-op `/status`.** The status routes discard the `advance_status` bool and commit
  unconditionally, so an ignored transition still returns 200.

## 4. P0 launch blockers (ordered)

1. **Toll-free deadlock.** No code writes the `carrier_refs["telnyx"]` that
   `_require_tfv_approved` demands, so a TFV can never be legitimately approved and toll-free
   numbers can never become send-eligible. Toll-free cannot launch.
2. **Stale approval trusted indefinitely.** Approval is verified only when recorded and the
   send path never re-checks the carrier, so a registration Telnyx later fails or suspends
   keeps sending locally, and terminal `approved` cannot be corrected.
3. **No real credentials / no live verification.** Production Telnyx credentials and an active
   provider account are external prerequisites, and no test yet exercises the real API (§6, §8).

## 5. P1 (non-blocking hardening)

- Return a distinguishable result when a `/status` transition is ignored (no-op 200).
- Surface `carrier_refs` on `TfvOut`; add a TFV attempt marker once its filing exists.
- Add a dedicated route regression test for the Telnyx order-path guard.
- Build the runbook for reconciling `carrier_refs`/`provisioning` markers; fix the stale
  docstrings in §1; note SQLite makes `with_for_update` a no-op (Postgres-only concurrency).
- Decide the `REQUIRE_NUMBER_REGISTRATION` default for non-Telnyx carriers.

## 6. Owner decisions, credentials and deploy (not code)

- Written authorization and budget for **billable, non-refundable** 10DLC filings; confirm the
  current fee/refund schedule from the carrier's published terms (no amounts asserted here).
- Production Telnyx credentials / active provider account(s), provisioned and rotated outside
  the repo (never in docs or logs); filing and the guards cannot work without them.
- Whether the manual, carrier-confirmed approval suffices, who is accountable, and the evidence.
- A named human process to reconcile timeout markers and periodically re-check
  already-`approved` registrations (P0 #2).
- Per-environment `REQUIRE_NUMBER_REGISTRATION` default; KYC scope; prepaid/settlement policy;
  BYON decision; pricing pass-through.
- Deploy window and rollback owner; a migration only if `carrier_refs` is proven insufficient.

## 7. Security risks (beyond missing features)

- **Stale carrier evidence is trusted.** The guards run once and the send gate reads only
  persisted rows, so after a carrier downgrade a number keeps sending on a now-false
  `approved` — and terminal `approved` cannot be demoted.
- **Unknown-registration allowance for non-Telnyx carriers:** allow-on-unknown is the default;
  the only tightening is an env flag, so the control is opt-in there.
- **Deadlock pressure to forge evidence.** With TFV approval needing a ref no code writes (P0
  #1), the workaround is hand-editing `carrier_refs` — out-of-band evidence the guard trusts.
- **Mock-only confidence.** A response-shape change in `registration_status` or TFV
  `verificationStatus` could mis-map and silently approve.

## 8. Verification: offline tests done, live carrier still missing

Offline tests (real service + `httpx.MockTransport`, no socket) are **done**: approval guards
(`test_telnyx_brand_approval_guard.py`, `test_telnyx_campaign_tfv_approval_guard.py`) cover
missing ref / carrier pending / mismatched id / non-`verificationStatus` field / confirmed
approval; number association (`test_telnyx_number_association_success.py` + `..._failures.py`)
covers success, pending, timeout marker, wrong-phone marker and repeat marker; payload tests
cover required-field and enum validation. **Still missing:** live-carrier verification, the
reconciliation runbook, a Telnyx order-path route regression test, and a TFV filing route.
