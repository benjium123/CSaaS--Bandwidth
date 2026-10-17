# 10DLC Phase 4: Move a number to another campaign - Implementation Plan

**Spec:** `docs/superpowers/specs/2026-10-17-tendlc-lifecycle-design.md` section 3.
**What exists:** `PATCH /api/v1/numbers/{id}/campaign` assigns a campaign; for a Telnyx number
it calls `telnyx_number_association.associate_number_with_telnyx`, which REFUSES a number that
already has a campaign (`_require_unassigned`). The UI (LineDetailPage note, NumbersPage
select) already tells customers they can move a number - the backend can't. New numbers are
auto-attached by `tendlc.associate_new_numbers` to the workspace's single active registration.

**Goal:** A Telnyx number already on campaign A can be moved to approved campaign B.

## Rulings
- Purchase/port-time picker is deferred: self-serve allows one registration per workspace, so
  there is one campaign to attach to. When a workspace holds MORE than one approved Telnyx
  campaign, `associate_new_numbers` does not guess: it leaves new numbers unassigned and the
  customer picks on the Numbers page (existing select).
- Telnyx transport (same base path the client already POSTs to):
  `DELETE /10dlc/phone_number_campaigns/{phoneNumber}` then
  `POST /10dlc/phone_number_campaigns {phoneNumber, campaignId}`. A 404 on DELETE means "not
  assigned at Telnyx" and is treated as success.

## Global Constraints
- Worktree `C:/Users/omer_/csaas_swg`, current branch; never `git stash` / `git checkout`.
- Backend tests `cd backend && python -m pytest -q <files> -p no:cacheprovider`; ruff.
  Frontend `npx vitest run <files>`, `npx tsc --noEmit -p .`.
- Never leave local and carrier disagreeing silently: every step records a `tendlc_events`
  row (subject "number", event_type `local.number.move`, payload_type "UNASSIGN"/"ASSIGN",
  status "OK"/"FAILED", reasons = error text) on the registration whose campaign is involved
  (target campaign's registration if any, else source's; registration_id may be NULL - record
  needs org_id only).
- Sole-proprietor 1-number limit: `registration_svc.ensure_campaign_has_room` before any
  carrier call (already in the route).

## Task 1 (one dispatch)
Backend:
1. `backend/app/providers/telnyx/registration.py`: `delete_phone_number_campaign(phone: str)`
   -> dict | None (None on 404), and `get_phone_number_campaign(phone)` -> dict | None.
   Follow the class's existing request/error style; add to the documented endpoints list.
2. `backend/app/services/telnyx_number_association.py`: new
   `async def move_number_to_campaign(session, settings, number, campaign, *, client=None)`.
   Same row-lock + approval gates as `associate_number_with_telnyx` (reuse its helpers). Steps:
   commit a durable marker `provisioning["telnyx_campaign_move"] = {state:"unassigning",
   from_campaign_id, to_campaign_id, attempted_at}`; DELETE at Telnyx; set
   `number.campaign_id = None`, clear `telnyx_campaign_assignment`, marker state
   "assigning", commit; then reuse `associate_number_with_telnyx` for the POST; on success
   drop the move marker. On a failure after the DELETE: keep `campaign_id = None`, marker
   state "failed" + error, record the FAILED event, raise a ValidationFailedError with plain
   words ("The number was removed from its old campaign but the new one refused it: ... It
   can't text until this is fixed.") and open `card_risk.open_alert(..., "tendlc_number_move_failed", {...})`.
3. `backend/app/api/routes/numbers.py` `assign_campaign`: for a Telnyx number whose
   `campaign_id` is set and differs from the payload -> `move_number_to_campaign`. Clearing
   (None) stays refused exactly as today.
4. `tendlc.associate_new_numbers`: if the org has >1 approved Telnyx campaign, do nothing (log
   `tendlc_numbers_need_choice`).
Tests (`backend/tests/test_number_campaign_move.py`, FakeTelnyx-style MockTransport; see
`tests/test_tendlc_self_serve.py` and existing association tests - grep
`associate_number_with_telnyx` in backend/tests): move happy path (DELETE then POST order,
campaign_id switched, 2 OK events); DELETE 404 treated as success; POST refused after DELETE ->
campaign_id None, marker failed, FAILED event, ops alert, 422 to the client; moving onto a full
sole-proprietor campaign -> refused before any carrier call; clearing still 409; two approved
campaigns -> associate_new_numbers assigns nothing.

Frontend:
5. Where the campaign select changes a number that already has a campaign (NumbersPage and
   LineDetailPage CampaignPicker): confirm first with the text "Moving this number pauses its
   texting until the carriers confirm the new campaign. Move it?" (use the page's existing
   confirm pattern, else `window.confirm`). Show the server's error message on failure.
   Update the related tests.

Commit: `feat(10dlc): move a number to another texting campaign (unassign then assign at Telnyx)`
