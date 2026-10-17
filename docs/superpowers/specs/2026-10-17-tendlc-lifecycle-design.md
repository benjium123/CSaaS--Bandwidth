# 10DLC lifecycle: status truth, customer notices, number-to-campaign, advisory review

Owner request 2026-10-17. Status: DRAFT for owner review.

## Goal
Customers (and we) always know where a texting registration stands: submitted, approved,
declined and WHY, with the fix suggested. Customers hear about every change by email and by an
in-app banner. They can choose which campaign a number texts under, and move it later. Before
submitting they get suggestions that make a rejection less likely. The suggestions are advice
only: a customer may ignore them, and the choice is theirs.

## What exists today (code facts, Oct 17)
- Real submission = a Telnyx id in `carrier_refs["telnyx"]`. The stages run checkout → paid →
  brand_filed → brand_approved → campaign_filed → active (plus rejected / needs_attention).
- Status is POLLED every 5 min (`tendlc.tick`). We send Telnyx NO webhook URL.
- On rejection we store a generic message only: the carrier's reason is lost, and the customer
  is not told.
- New and ported numbers attach automatically to the workspace's single active campaign
  (`associate_new_numbers`). A failure goes to the log only. There is no choice of campaign and
  no way to move a number.
- The forms lack: privacy policy URL, terms URL, opt-in / STOP / HELP keywords, opt-in proof and
  expected volume.
- Checks before submit are required-field checks only.
- Building blocks we reuse: the `notifications` table (`services/notifications.py`), the shell
  banner arbitration (`components/shell/BannerSlot.tsx`), `billing_alerts.notify_owners`
  (email), and Telnyx ed25519 webhook verification (`providers/telnyx/webhooks.py`).

## Telnyx facts (docs research Oct 17; see "Unverified")
- Event types: `10dlc.brand.update`, `10dlc.campaign.update`, `10dlc.phone_number.update`.
  Envelope: `data {event_type, id, occurred_at, payload}`.
- Campaign payload types include `TELNYX_REVIEW` and `MNO_REVIEW` (ACCEPTED/REJECTED),
  `REGISTRATION`, `VERIFIED`, `TELNYX_EVENT` (DORMANT). Number payload types: `ASSIGNMENT`,
  `DELETION`, `STATUS_UPDATE`.
- Reasons arrive in `reasons[]` (field + description), or in `description` on an MNO rejection.
  `GET /10dlc/campaignBuilder/{id}` gives `status` and `mnoMetadata`, but no reason text.
- Number move = `DELETE /10dlc/phoneNumberCampaign/{phone}` then
  `POST /10dlc/phoneNumberCampaign {phoneNumber, campaignId}`. A number sits on one campaign at a
  time.
- Telnyx offers NO pre-submission review service, so we build our own advisory check.

## Design

### 1. Status truth (backend)
- New route `POST /api/v1/webhooks/telnyx/10dlc`.
  - Verify the ed25519 signature when the headers are present. Either way, treat the webhook as
    a TRIGGER: look up the brand/campaign/number by Telnyx id, re-read its status from the
    Telnyx API, and apply that. The payload is never trusted for status, so an unsigned or
    forged call can only cause a harmless re-check.
  - Take the reason text (`reasons[]` / `description`) from the payload. If the signature did not
    verify, store it marked `unverified`.
- New table `tendlc_events`: org_id, registration_id, subject (brand|campaign|number), telnyx_id,
  event_type, payload_type, status, reasons (json), verified (bool), raw (json), received_at.
  This is the timeline the customer and ops both see.
- The registration keeps the last reason: `detail.rejection = {source, reasons[], at}`. It
  replaces today's generic text, with the generic text kept as a fallback.
- Send `webhookURL` / `webhookFailoverURL` when filing a brand and campaign, and backfill them on
  existing ones with an update call. The 5-min poll stays as the safety net.
- A failed number assignment writes a `tendlc_events` row with the reason, notifies the customer
  (section 2), and raises an ops alert. It is retried on the next pass as today.

### 2. Customer notices
- On any stage change, or on a number assignment success or failure:
  - an email to the workspace owners and admins (`notify_owners`), in plain words, with the
    reason, the suggested fixes and a link;
  - one `notifications` row per owner/admin (kind `texting`, dedupe per event).
- New shell banner `TextingStatusBanner` in `BannerSlot` (priority 25: after credits, before
  verification). It shows the newest unread texting notice, e.g. "Your texting campaign was
  approved" or "Your texting campaign needs changes: <reason> → Review". It stays until the user
  opens the registration page or dismisses it.
- The registration page shows the timeline from `tendlc_events` and, on a rejection, the reason
  with the suggested fix next to the field it concerns.

### 3. Number ↔ campaign
- Buying or porting a number: if the workspace has more than one active campaign, show a
  "Texting campaign" picker. The choice is stored on the purchase or port, and
  `associate_new_numbers` uses it. With exactly one active campaign it attaches automatically,
  as today.
- The number's page shows a "Texting campaign" row: the current campaign, the assignment status,
  and a "Move to another campaign" button.
  - A move runs DELETE then POST at Telnyx, and records each step in `tendlc_events`.
  - The UI warns first: texting from this number pauses until the carriers confirm the new
    campaign. Telnyx does not document how long that takes.
- A sole-proprietor campaign keeps its 1-number limit, enforced before calling Telnyx.

### 4. Form fields + advisory review
- Add the fields: privacy policy URL, terms URL, opt-in keywords, opt-out keywords, help
  keywords, opt-in proof (an uploaded screenshot or a URL), expected monthly messages. Send
  each to Telnyx where the campaign payload accepts it (exact names verified at build time);
  otherwise put it into the description / message-flow text the vetters read.
- New "Check my campaign" step before submit:
  - Rule checks: a sample message missing the brand name or "Reply STOP", a URL shortener, an
    opt-in description that never says how consent is collected, a use case that doesn't match
    the samples, the website or privacy URL not reachable, a public-facing email on a free
    domain, and similar.
  - Then one LLM pass (Haiku) over the whole filing against the carrier rules. Output is a list
    of suggestions, each {severity: likely-rejection | improve, field, why, suggested text}.
  - Never blocking. "Submit anyway" records the shown suggestions and the user's choice in the
    audit log and on the registration, so later we can show "we suggested X".
  - The cost is ours: about 1 cent per check. Limit 10 checks per registration per day.

### 5. Switchboard
- Trust & safety → Texting registrations lists ALL registrations, not only stuck ones: workspace,
  stage, brand/campaign Telnyx ids, last event and its time, the reason, and how many numbers are
  assigned / failed. A row opens the timeline.

## Build order (each a phase with its own tests; deploy after each)
1. Status truth: webhook, events table (migration), saved reasons, webhook URLs on file/update.
2. Customer notices: email, notification rows, banner, timeline on the registration page.
3. Switchboard list.
4. Number ↔ campaign: picker at purchase/port, move on the number page.
5. Form fields + advisory review.

## Unverified (checked at build time, before code depends on it)
- The exact Telnyx field names for the webhook URL on brand/campaign. The docs conflict, so one
  test update on our own brand settles it.
- Whether 10DLC webhooks carry ed25519 signatures. The trigger-only design is safe either way.
- How long texting is down while a number moves between campaigns, and whether Telnyx charges
  for a move.
- Which of the new fields Telnyx's campaignBuilder accepts as named fields.

## Out of scope
- Toll-free verification (separate flow).
- Any change to who may text unregistered (individuals stay hard-gated).
- Refund rules on rejection (unchanged).
