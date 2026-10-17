# 10DLC Phase 3: Switchboard registrations list - Implementation Plan

**Spec:** `docs/superpowers/specs/2026-10-17-tendlc-lifecycle-design.md` section 5.
**Goal:** Trust & safety → Texting registrations shows, for every registration: workspace, stage,
brand/campaign Telnyx ids, the last carrier event and its time, the saved reason, and numbers
assigned / failed. A row opens its timeline. Ops can filter by stage.

## Global Constraints
- Worktree `C:/Users/omer_/csaas_swg`, current branch; never `git stash` / `git checkout`.
- Backend `cd backend && python -m pytest -q <files> -p no:cacheprovider`; ruff. Frontend
  `cd frontend && npx vitest run <files>`; `npx tsc --noEmit -p .`.
- Operator reads stay `Reader` (ops:read); unscoped queries carry
  `.execution_options(**{ALLOW_UNSCOPED_KEY: True})` with a `# JUSTIFIED:` comment like the
  existing list. Never return the webhook `raw` payload.
- Keep the existing Reconcile / Cancel buttons and their rules exactly.

### Task 1 (one dispatch: backend + frontend)
Backend `backend/app/api/routes/ops.py`:
- `GET /texting-registrations` rows gain: `brand_telnyx_id`, `campaign_telnyx_id` (from
  `carrier_refs["telnyx"]`), `rejection` (reg.rejection or null), `brand_reused`
  (filing flag), `last_event` (`{event_type, status, received_at, verified}` of the newest
  `tendlc_events` row, or null), `numbers_assigned` (OrgNumber rows of the org with
  `campaign_id == reg.campaign_id`), `numbers_failed` (distinct telnyx_id of FAILED number
  events for the reg). Do it with grouped queries over the listed ids, not one query per row.
  Optional `?stage=` filter (exact match), limit stays 100.
- New `GET /texting-registrations/{reg_id}/events` (Reader): newest first, max 100, fields as
  the customer endpoint (`id, subject, telnyx_id, event_type, payload_type, status, reasons,
  verified, received_at`). 404 for an unknown id.
- Tests in `backend/tests/test_ops_texting_registrations.py` (create; reuse helpers from
  `tests/test_tendlc_self_serve.py` and the operator-client fixture used by existing ops tests —
  grep `texting-registrations` in backend/tests): fields present, last_event newest, failed
  numbers counted, stage filter, events endpoint order + 404, non-operator 403.

Frontend `frontend/src/pages/OpsPage.tsx` `TextingRegistrationsTab` (+ type):
- Lede → "Every self-serve 10DLC filing, newest first. Stuck ones need you."
- A stage `<select>` filter (All + the stages present) - client side is fine.
- Each row adds: Telnyx brand / campaign ids (monospace, small), last event label + relative
  or ISO time, numbers "N on campaign · M failed", the rejection reasons (text, prefixed
  "Carrier: ", and "(unsigned)" when not verified), "Resubmit (no brand fee)" tag when
  brand_reused.
- A "Timeline" toggle per row that fetches `/texting-registrations/{id}/events` (useOps) and
  lists date · subject · status · reasons.
- Update `frontend/src/pages/OpsTextingRegistrations.test.tsx` for the new fields/toggle (the
  area toggle "Trust & safety" must be opened first - see the existing test).

Commit: `feat(ops): texting registrations list shows carrier ids, last event, reasons, numbers and a timeline`
