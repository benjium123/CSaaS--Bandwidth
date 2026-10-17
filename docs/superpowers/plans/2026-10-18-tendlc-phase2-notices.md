# 10DLC Phase 2: Customer Notices - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Every 10DLC stage change and every failed number assignment reaches the workspace
owners/admins by email and as an in-app notification. A top banner shows the newest unread
texting notice with a link to Settings → Messaging. The registration card shows the timeline.

**Spec:** `docs/superpowers/specs/2026-10-17-tendlc-lifecycle-design.md` section 2 (and the
ops-alert line of section 1). Phase 1 (`tendlc_events`, `reg.rejection`, `handle_carrier_event`)
is merged.

## Global Constraints
- Worktree `C:/Users/omer_/csaas_swg`, current branch. Never `git stash` / `git checkout`.
- Backend tests: `cd backend && python -m pytest -q <files> -p no:cacheprovider`; `ruff check`.
- Frontend tests: `cd frontend && npx vitest run <files>`; `npx tsc --noEmit -p .`.
- No new dependencies. No migration is needed (notification `kind` is a plain String(16)
  validated in code by `NOTIFICATION_KINDS`).
- Customer copy: plain words, no carrier jargon. Notice bodies <= 255 chars (the column).
- Link target for every texting notice: `/settings/messaging`.
- A notice is sent once per (registration, stage) - dedupe key `tendlc:<reg_id>:<stage>`;
  number failures `tendlc:<reg_id>:num:<e164>`.
- Notices never break the status machine: any notice error is logged and swallowed.

## Review Focus
1. Two ticks in a row at the same stage -> exactly one notification per owner (dedupe).
2. A workspace with no owner email -> notification rows still written, no crash.
3. Stage moves that the customer should NOT be pinged for (`paid` -> internal, `checkout`,
   `expired`, `cancelled`) -> no notice.
4. Banner dismiss marks the notification read and it does not come back on reload.
5. Notice email/notification for a rejection includes the carrier reason (phase 1
   `rejection_text`) and the refund/stop text already in `reg.detail`.

---

### Task 1: Backend notices service + hook

**Files:**
- Modify: `backend/app/models/inbox_pro.py` - add `"texting"` to `NOTIFICATION_KINDS`.
- Modify: `backend/app/services/billing_alerts.py` - `notify_owners(..., *, dedupe_key,
  kind: str = "low_balance")`, pass `kind` to `notifications.create`. Default keeps every
  existing caller unchanged.
- Create: `backend/app/services/tendlc_notices.py`
- Modify: `backend/app/services/tendlc.py` - in `advance()` capture the stage before `_step`
  and, after the commit, call `tendlc_notices.stage_changed(...)` when it changed; in
  `associate_numbers`' failure branch (phase 1 added the event record there) also call
  `tendlc_notices.number_failed(...)` and `card_risk.open_alert(session, org_id,
  "tendlc_number_failed", {"registration_id", "number", "error"})`. Also hook
  `handle_event` (-> paid is NOT notified) and `verify_otp` (-> brand_filed IS notified) only
  if they don't go through `advance`; read the code and hook where the stage is set.
- Test: `backend/tests/test_tendlc_notices.py`

**Interfaces (produce exactly):**
```python
# tendlc_notices.py
NOTIFY_STAGES: dict[str, str]  # stage -> subject
async def stage_changed(session, settings, reg, old_stage: str, new_stage: str) -> bool
async def number_failed(session, settings, reg, e164: str, error: str) -> bool
def body_for(reg, stage: str) -> str   # email body, plain text, ends with the link line
```
Subjects (verbatim):
- `brand_filed`: "Your texting registration was submitted"
- `brand_approved`: "Your business was approved for texting"
- `brand_rejected`: "Your texting registration needs changes"
- `campaign_filed`: "Your texting campaign was submitted for carrier review"
- `active`: "Your texting campaign is approved - you can text now"
- `campaign_rejected`: "Your texting campaign needs changes"
- `needs_attention`: "Your texting registration needs attention"
- number failure: "A number could not be added to your texting campaign"

Body: subject sentence, then `reg.detail` when present (it already carries the carrier
reason + refund/stop text from phase 1), then
`Review it here: {settings.public_base_url or ""}/settings/messaging`. The in-app
notification body is the subject, plus `": " + first carrier reason` for rejections, cut to
255. Org is loaded with the unscoped option the codebase uses; `notify_owners` is called with
`kind="texting"`, `dedupe_key=f"tendlc:{reg.id}:{new_stage}"`. Returns False (and sends
nothing) for stages not in NOTIFY_STAGES. Wrap the whole body in try/except -> log
`tendlc_notice_failed`, return False.

**Tests (write first, all must pass):**
- `test_active_notifies_owner_once`: drive a registration to `active` with the phase-1 test
  helpers (`tests/test_tendlc_webhook.py::_to_campaign_filed` style, FakeTelnyx
  `campaign_status={"campaignStatus":"MNO_PROVISIONED"}`), tick twice; exactly one
  `Notification(kind="texting")` for the owner; the stubbed mailer got one email whose
  subject contains "you can text now". Monkeypatch the mailer the way
  `tests/test_billing_alerts*.py` does (find it with grep `mailer`).
- `test_rejection_notice_carries_the_reason`: phase-1 webhook rejection with reason
  "Opt-in is not described" -> notification body contains it; email body contains it and
  `/settings/messaging`.
- `test_paid_is_silent`: checkout -> paid produces no texting notification.
- `test_failed_assignment_notifies_and_opens_ops_alert`: `telnyx.fail_assignment=True` ->
  one texting notification, one `SecurityAlert(kind="tendlc_number_failed")`.
- `test_notify_owners_default_kind_unchanged`: existing low-balance tests still pass (run
  `tests/test_billing_alerts*.py`).

Commit: `feat(10dlc): email + in-app notice on every texting registration change; ops alert on failed number`

---

### Task 2: Timeline API

**Files:**
- Modify: `backend/app/api/routes/registration.py` - `GET /registration/texting/{reg_id}/events`
  with the same permission as `GET /registration/texting` (compliance:read). Returns
  `{"items": [{id, subject, telnyx_id, event_type, payload_type, status, reasons, verified,
  received_at}]}` newest first, max 50, only rows of this org's registration (404 otherwise).
  Never return `raw`.
- Also in that GET handler, mark the caller's unread `kind="texting"` notifications read
  (opening the registration page clears the banner) - via `notifications.mark_read` with
  the ids, inside the same request.
- Test: `backend/tests/test_tendlc_notices.py` (append): events listed newest first;
  another org's registration id -> 404; `raw` absent; GET texting marks texting notices read.

Commit: `feat(10dlc): registration timeline endpoint; opening it clears texting notices`

---

### Task 3: Frontend banner + timeline

**Files:**
- Modify: `frontend/src/components/shell/BannerSlot.tsx` - `BANNER_PRIORITY.texting = 25`.
- Create: `frontend/src/components/registration/TextingStatusBanner.tsx` (+ `.test.tsx`)
- Modify: the shell file that renders `LowBalanceBanner` (grep it) - render
  `<TextingStatusBanner />` next to it.
- Modify: `frontend/src/api/registration.ts` - `TextingEvent` type +
  `useTextingEvents(regId)` (react-query, same client pattern as `useTextingRegistration`).
- Modify: `frontend/src/components/registration/TextingRegistrationCard.tsx` - an "Activity"
  list under the progress steps: date, plain label (Submitted / Approved / Needs changes /
  Number not added), reasons as text (never HTML). Hidden when there are no events.

Banner behaviour: reads `/me/notifications` through the same query hook/key the
NotificationBell uses (reuse it, don't add a second poller); picks the newest unread
`kind === "texting"`; renders inside `<BannerSlot priority={BANNER_PRIORITY.texting}>` the
body text + a "Review" link to `/settings/messaging` + a dismiss button that POSTs
`/me/notifications/read {ids:[id]}` and invalidates the notifications query. Hidden (returns
null, no claim) when there is none. Tone: rejected/needs-changes words -> warning style,
otherwise info style (match LowBalanceBanner's classes).

Tests: shows newest unread texting notice with the Review link; hidden with none; dismiss
calls the read endpoint; timeline renders reasons as text.
Run the shell/registration test files plus `npx tsc --noEmit -p .`.

Commit: `feat(10dlc): texting status banner and registration activity timeline`

---

### Task 4: Release (controller)
Backend + frontend suites for touched areas; push main; wait for API image; `deploy-api.sh`;
frontend build + dist rsync (back up dist first). Live: GET /me/notifications works.
