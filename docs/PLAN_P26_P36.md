# Plan P26–P36 — Enterprise completeness (expanded from Fable outline, 2026-09-10)

## Order and dependencies
- P26 is the first slice and is independent; it builds only on the existing inbox and notification primitives.
- P27–P29 build on existing contacts, messaging, voice, and provider-account services and should follow P26.
- P30 depends on P19/P24 spend data and the P23 assistant usage tables; P31 depends on P30 email settings.
- P32 depends on the P24 credit ledger and usage metering; P33 depends on P32 invoicing.
- P34–P36 depend on P26–P33 surfaces, with P36 closing the security questionnaire after all earlier features are observable.
- Each phase is a vertical slice and may deploy independently; the order above is the intended merge order.

# Phase 26 — Inbox pro (WS-7 + WS-2)

## Goal
The inbox becomes a team tool: private notes, mentions, quick replies, snooze, and SLA targets. Agents work faster inside the thread instead of switching to external tools, and leads get insight into first response and resolution time.

## Pre-dependencies
None new. Migration `0028_inbox_pro` (Fable).

## Schema (migration 0028, Fable-only)
- `thread_notes` — id, org_id, thread_id, author_user_id, body TEXT, mentions JSONB
  DEFAULT '[]'::jsonb, created_at TIMESTAMPTZ; FK thread_id → message_threads ON DELETE
  CASCADE, author_user_id → users ON DELETE SET NULL; index `(org_id, thread_id,
  created_at)`.
- `message_threads.snoozed_until` TIMESTAMPTZ NULL; `first_response_at` TIMESTAMPTZ
  NULL; `sla_breached_at` TIMESTAMPTZ NULL; index `(org_id, snoozed_until)`;
  index `(org_id, sla_breached_at)`.
- `inboxes.sla_first_response_minutes` INT NULL; `inboxes.sla_resolution_minutes` INT
  NULL; both nullable, zero/negative invalid at API level.
- `notifications` — id, org_id, user_id, kind VARCHAR(32)
  (`mention`|`assignment`|`overdue`|`missed_call`), thread_id UUID NULL, body TEXT,
  read_at TIMESTAMPTZ NULL, created_at TIMESTAMPTZ; FK thread_id → message_threads ON
  DELETE CASCADE, user_id → users ON DELETE CASCADE; index `(org_id, user_id,
  read_at)`.

## Permissions (if any new keys)
No new permission keys. Existing `inbox:send` gates note creation and snooze; existing
contact/thread visibility still applies.

## Features (customer-facing names in quotes)
- Composer gains one toggle: "Reply" or "Note". A note is private, renders as a yellow
  card in the timeline, and is not sent to the contact.
- "@ mention" in notes and replies autocompletes org members with "Can use inbox"
  permission; it creates a notification row and a bell entry in the shell.
- "Quick reply" — typing "/" in the composer searches existing templates with merge
  fields; no new canned-response entity is created.
- "Snooze" — one menu with choices "1 hour", "3 hours", "Tomorrow 9:00", "Next week",
  and "Custom". Snoozed threads leave the list and return automatically via the
  sweeper when `snoozed_until` is reached.
- "Inbox SLA" — per-inbox first-response and resolution targets live in Settings →
  Departments & Inboxes. The timeline shows a small countdown chip; breach writes
  `sla_breached_at` and a notification to inbox leads. An "Overdue" filter chip
  appears in Inbox.
- "Bell" — shell menu groups mentions, assignments, overdue SLA, and missed calls from
  P16 call events. Unread count clears individually and from the menu.
- Missed-call notification rows are derived from P16 call events that terminate
  unanswered and assigned to an inbox the user can read.
- Search fix: when matching a transcript, the `matched` flag is set on every message
  whose body actually matched. Closes D25.

## Simplification
- Remove the separate "Templates" picker button; template insertion is folded into the
  "/" quick-reply path.
- The Open / Unread / Unresponded / Important chips become a single "Filter" dropdown
  when the org has more than five filter chips in the inbox header.
- No new top-level navigation; all features live inside the existing Inbox and Settings
  surfaces.

## Allowed files (backend and frontend)
Backend: `backend/app/models/messaging.py` (MessageThread columns),
`backend/app/models/inboxes.py` (SLA target columns),
`backend/app/models/notifications.py` (new), `backend/app/services/notifications.py` (new),
`backend/app/services/inbox.py`, `backend/app/services/sweeper.py`,
`backend/app/services/search.py` (D25 matched flag),
`backend/app/api/routes/inbox.py`, `backend/app/api/routes/inboxes.py`,
`backend/app/api/routes/conversations.py`,
`backend/app/api/routes/notifications.py` (new), `backend/tests/test_p26_*.py`.
Request/response models are declared inline in the route modules; the repo has no
`schemas` package and this phase does not introduce one.
Frontend: `frontend/src/pages/InboxPage.tsx`,
`frontend/src/components/inbox/Composer.tsx`,
`frontend/src/components/inbox/ThreadList.tsx`,
`frontend/src/components/inbox/ThreadView.tsx`,
`frontend/src/components/inbox/QuickReplyMenu.tsx` (new),
`frontend/src/components/inbox/SnoozeMenu.tsx` (new),
`frontend/src/components/inbox/SlaChip.tsx` (new),
`frontend/src/components/shell/BellMenu.tsx` (new),
`frontend/src/components/shell/Sidebar.tsx` (bell mount point),
`frontend/src/pages/InboxSettingsPage.tsx` (SLA targets),
`frontend/src/api/notifications.ts` (new), `frontend/src/api/conversations.ts`,
matching `*.test.tsx`.

## Forbidden
- `backend/app/services/inbox_access.py` — P15 grant logic may be read, never changed.
- Migrations: `backend/migrations/versions/**` (Fable writes `0028_inbox_pro`).
- Money and ledger code from P24: `backend/app/models/billing.py`,
  `backend/app/services/credits.py`, `backend/app/services/ai_usage.py`,
  `backend/app/services/stripe_client.py`.
- `backend/app/models/rbac.py` — this phase adds no permission keys.
- `.env`, `deploy/**`, `backend/app/auth/deps.py`.
- No new top-level navigation.

## Test spec
Unit:
- [ ] note_create_is_private_and_does_not_send_sms — a note persists in `thread_notes`
      and no outbound message row is created.
- [ ] mention_creates_notification_and_bell_unread — a note/reply containing an
      @mention inserts a notification for the target member.
- [ ] snooze_removes_thread_from_visible_list_and_sweeper_returns — a snoozed thread
      is filtered out until `snoozed_until` passes.
- [ ] sla_first_response_set_once_on_outbound_reply — first inbound message gets
      `first_response_at` when an agent replies first.
- [ ] sla_breach_writes_timestamp_and_notifies_inbox_leads — crossing
      `sla_first_response_minutes` sets `sla_breached_at`.
- [ ] overdue_filter_returns_breached_threads_only — the filter predicate matches only
      breached, non-resolved threads.
- [ ] mention_rejects_non_member_or_no_inbox_send — a mention target outside the org
      or without inbox permission is rejected with 422.
- [ ] snooze_custom_rejects_past_time — custom snooze before now returns 422.
- [ ] transcript_search_matched_flag_is_correct — the matched flag is set only on
      messages whose body matched, and is not reused across messages. Closes D25.
Integration:
- [ ] mention_to_snooze_to_notification_flow — an agent mentions a lead; the lead
      snoozes the thread; the sweeper returns it without losing the notification.
- [ ] sla_breach_appears_in_bell_and_overdue_filter — a thread under a 1-minute SLA
      breaches and appears in both the bell and Overdue filter.
Frontend:
- [ ] composer_reply_note_toggle_persists_per_draft — switching between Reply and
      Note does not clear the opposite draft.
- [ ] quick_reply_opens_slash_menu_and_fills_merge_fields — typing "/" searches
      templates and inserts the selected template body.
- [ ] bell_menu_marks_single_notification_read — clicking one item clears that item
      and leaves others unread.
- [ ] filter_dropdown_replaces_chips_above_five — six or more chips collapse into one
      dropdown.
Manual: log in as two org members in the same inbox, mention one another, snooze a
thread, and confirm the snoozed thread disappears and returns via the sweeper.

## Deploy
yes — migration 0028 is additive; default SLA values NULL mean no behaviour change
until an admin sets targets.

---

# Phase 27 — Contacts pro + data lifecycle (WS-7 + WS-0)

## Goal
Contacts become trustworthy and manageable: export, duplicate detection and merge,
saved views, retention policies, and subject erasure requests. Administrators can
satisfy “right to delete” requests without losing the compliance ledger trail.

## Pre-dependencies
Migration `0029_contacts_lifecycle` (Fable).

## Schema (migration 0029, Fable-only)
- `contacts.merged_into_contact_id` UUID NULL FK contacts ON DELETE SET NULL;
  index `(org_id, merged_into_contact_id)`.
- `saved_views` — id, org_id, user_id UUID NULL (NULL = shared), name VARCHAR(120),
  filters JSONB NOT NULL, sort JSONB NOT NULL DEFAULT '{"field":"created_at",
  "dir":"desc"}'::jsonb, created_at TIMESTAMPTZ; FK org_id, user_id; unique
  `(org_id, user_id, name)`.
- `retention_policies` — org_id PK/FK, messages_days INT NULL, recordings_days INT
  NULL, transcripts_days INT NULL, imports_days INT NULL; NULL means keep forever;
  non-null values > 0.
- `erasure_requests` — id, org_id, contact_id, requested_by UUID, status VARCHAR(12)
  `pending`|`done`, completed_at TIMESTAMPTZ NULL, summary JSONB DEFAULT '{}'::jsonb,
  created_at; FK contact_id, requested_by; index `(org_id, status)`.

## Permissions (if any new keys)
No new permission keys. `contacts:read`/`contacts:read_all` gate export and views;
`compliance:manage` gates erasure requests; `contacts:write` gates merge.

## Features (customer-facing names in quotes)
- "Export contacts" — CSV export respects P22 visibility; runs as an async job with a
  download link; streaming output prevents memory blow-up.
- "Possible duplicates" — nightly job finds same normalized name + email or shared
  phone label; the contact detail shows a "Possible duplicates" panel. Merge
  re-parents phones, tags, notes, and threads to the survivor; the loser row stays
  with `merged_into_contact_id`.
- "Saved views" — from the Contacts filter bar, save the current filters as "My view"
  or "Shared view". Saved views appear as chips above the contact list and are
  clickable without leaving Contacts.
- "Data retention" — Settings → Workspace → Data has four numbers: messages keep
  forever by default, recordings 90 days, transcripts 365 days, imports 30 days. The
  sweeper purges expired data and deletes import blobs. Closes D4.
- "Erase this person" — on a contact detail, an erasure request anonymizes contact
  fields, message bodies, and transcripts, deletes recordings, and keeps opt-out and
  compliance ledger rows. Every erasure writes an audit entry.
- "Export my data" — same export path scoped to one contact: CSV plus a JSON bundle of
  the contact’s owned records.
- Merge is deterministic because phone uniqueness already prevents ambiguous phone
  re-parenting; the survivor is the contact chosen in the merge action.

## Simplification
- The standalone Lists page merges into Contacts as a "Lists" tab. A list is a saved
  view plus membership; remove Lists from the top-level navigation.
- No new top-level nav; duplicate, retention, and erasure controls live inside contact
  detail and Settings.

## Allowed files (backend and frontend)
Backend: `backend/app/models/contacts.py` (merge column, contact_notes),
`backend/app/models/saved_views.py` (new), `backend/app/models/privacy.py` (new;
retention_policies + erasure_requests), `backend/app/services/contact_lifecycle.py` (new),
`backend/app/services/retention.py` (new), `backend/app/services/privacy.py` (new),
`backend/app/services/contacts.py`, `backend/app/services/list_import.py`,
`backend/app/services/sweeper.py`, `backend/app/api/routes/contacts.py`,
`backend/app/api/routes/orgs.py` (retention settings),
`backend/app/api/routes/compliance.py`, `backend/tests/test_p27_*.py`.
Request/response models inline in the route modules (no `schemas` package).
Frontend: `frontend/src/pages/ContactsPage.tsx`,
`frontend/src/pages/ListsPage.tsx` (route removed; content becomes the Contacts "Lists" tab),
`frontend/src/components/contacts/SavedViewChips.tsx` (new directory),
`frontend/src/components/contacts/DuplicatesPanel.tsx` (new),
`frontend/src/components/contacts/MergeDrawer.tsx` (new),
`frontend/src/components/contacts/EraseContactDrawer.tsx` (new),
`frontend/src/pages/SettingsWorkspaceDataPage.tsx` (new),
`frontend/src/components/shell/Sidebar.tsx` (drop the Lists entry),
`frontend/src/api/contacts.ts` (new), `frontend/src/api/privacy.ts` (new),
matching `*.test.tsx`.

## Forbidden
- Any erasure path that deletes opt-out consent rows or compliance ledger rows —
  opt-out must survive erasure.
- In-memory CSV generation; export must stream to storage.
- Migrations: `backend/migrations/versions/**` (Fable writes `0029_contacts_lifecycle`).
- Money and ledger code from P24: `backend/app/models/billing.py`,
  `backend/app/services/credits.py`, `backend/app/services/ai_usage.py`,
  `backend/app/services/stripe_client.py`.
- `backend/app/models/rbac.py` — this phase adds no permission keys.
- `.env`, `deploy/**`, `backend/app/auth/deps.py`.
- No new top-level navigation.

## Test spec
Unit:
- [ ] export_csv_respects_visibility_rule — a department-restricted export excludes
      contacts the requester cannot see.
- [ ] duplicate_detection_matches_normalized_name_email_or_phone — nightly job finds
      expected duplicate pairs and ignores distinct contacts.
- [ ] merge_reparents_phones_tags_notes_threads — survivor owns loser’s related rows;
      loser has `merged_into_contact_id` set.
- [ ] merge_keeps_loser_row_and_does_not_orphan_phones — no phone or thread is
      orphaned or physically deleted.
- [ ] saved_view_shared_and_private_scopes_are_enforced — a user sees shared views
      plus their own, never another user’s private views.
- [ ] retention_policy_rejects_zero_and_negative_values — API returns 422 for invalid
      non-null values.
- [ ] retention_sweeper_purges_import_blobs — expired import rows have their backing
      objects deleted and rows updated. Closes D4.
- [ ] erasure_anonymizes_contact_messages_and_transcripts — personal fields are
      replaced, recordings deleted, and the row remains for ledger/reference.
- [ ] erasure_keeps_opt_out_consent_rows — opt-out rows survive an erasure request.
- [ ] erasure_request_requires_compliance_manage — a non-admin agent gets 403.
- [ ] export_my_data_scoped_to_one_contact — another contact’s records are never
      included.
Integration:
- [ ] duplicate_detection_to_merge_flow — nightly duplicate appears, admin merges, and
      all threads/tags/phones are visible on the survivor.
- [ ] retention_and_erasure_flow — set 30-day message retention, force sweeper, then
      request erasure for one contact and confirm only that contact is anonymized.
Frontend:
- [ ] contacts_saved_view_chip_applies_filters — clicking a saved-view chip sets the
      filter state and issues one list request, with no route change.
- [ ] duplicate_panel_shows_merge_preview — survivor/loser fields are visible before
      confirming merge.
- [ ] erase_drawer_requires_confirmation — the user must type the contact name.
- [ ] lists_tab_inside_contacts — standalone Lists nav is gone and lists render as a
      tab.
Manual: search a contact with a shared phone, merge two duplicates, then confirm the
survivor shows merged threads and the loser redirects to the survivor.

## Deploy
yes — migration 0029 is additive; default retention NULL preserves current behavior
until admin changes settings.

---

# Phase 28 — Messaging completeness (WS-2)

## Goal
MMS works end to end, send-later is reliable, links can be tracked, and failure
reasons are plain and useful. One composer handles SMS, MMS, and scheduled sends
without confusing the agent.

## Pre-dependencies
Migration `0030_messaging_completeness` (Fable).

## Schema (migration 0030, Fable-only)
- `short_links` — id, org_id, code VARCHAR(32) UNIQUE, target_url TEXT, message_id
  UUID NULL, contact_id UUID NULL, clicks INT NOT NULL DEFAULT 0, created_at; index
  `(org_id, code)`, `(org_id, message_id)`.
- `link_clicks` — id, short_link_id FK, at TIMESTAMPTZ, ip_hash VARCHAR(64),
  user_agent TEXT; index `(short_link_id, at)`.
- `messages.scheduled_for` TIMESTAMPTZ NULL — single-message send-later, distinct from
  campaign schedule; index `(org_id, scheduled_for)`.
- `messages.failure_reason_public` VARCHAR(255) NULL — plain-language reason shown on
  the bubble.

## Permissions (if any new keys)
No new permission keys. Sending is gated by the existing `inbox:send`; the org-level
link-tracking default and quiet-hours settings use `settings:write`.

## Features (customer-facing names in quotes)
- "MMS attachments" — attach image or PDF in the composer. The media service stores
  the attachment; size/format guards return provider-specific plain text such as
  “Attachments up to 3.5 MB”. If the provider rejects MMS, the send falls back
  to SMS with a link to the attachment.
- "Send later" — clock icon in the composer picks a time in the contact’s timezone,
  with quiet hours enforced. Scheduled messages wait until the sweeper/dialer sends
  them and are visible in the timeline as pending.
- "Link tracking" — any URL in a message can be shortened at send time using
  `PUBLIC_WEB_URL/l/{code}`. Organization setting defaults tracking on or off. The
  public URL redirects and records a click; the timeline shows “Clicked 2×”.
- "Delivery details" — provider error codes map to plain words in one table inside
  `services/messaging` errors. The message bubble shows a friendly reason such as
  “This number can’t receive texts (landline)”.
- Attachment fallback never sends an unsupported MMS; it sends the SMS fallback and
  stores the original media link.

## Simplification
- One composer now serves SMS, MMS, and send-later. There is no separate "Media" page
  in the app today, so nothing is removed from the nav here; what goes away inside the
  composer is the three separate affordances — attach, schedule, insert template — which
  collapse into one toolbar with a single "More options" disclosure, collapsed by default
  (Collapsible + storageKey). The template button removed in P26 stays removed.
- Fable decision (2026-09-10): the real removal in this phase is the Campaigns page's own
  message editor and media uploader. Campaigns reuse the shared Composer component
  (same attach / send-later / "/" template search), so one composer exists in the app
  and the campaign-specific editor code is deleted. Test: campaigns_use_shared_composer
  (CampaignsPage renders Composer; the old editor component no longer exists).

## Allowed files (backend and frontend)
Backend: `backend/app/models/messaging.py` (`scheduled_for`,
`failure_reason_public`), `backend/app/models/short_links.py` (new),
`backend/app/services/messaging.py` (plain-words failure table),
`backend/app/services/media.py`, `backend/app/services/outbox.py`,
`backend/app/services/sender.py`, `backend/app/services/sweeper.py`,
`backend/app/api/routes/messages.py`, `backend/app/api/routes/media.py`,
`backend/app/api/routes/short_links.py` (new; the `/l/{code}` redirect is mounted
outside the authenticated API prefix), `backend/tests/test_p28_*.py`.
Request/response models inline in the route modules (no `schemas` package).
Frontend: `frontend/src/components/inbox/Composer.tsx`,
`frontend/src/components/inbox/SendLaterPicker.tsx` (new),
`frontend/src/components/inbox/AttachmentButton.tsx` (new),
`frontend/src/components/conversations/Timeline.tsx` (failure text, click count),
`frontend/src/pages/InboxPage.tsx`, `frontend/src/api/messages.ts` (new),
`frontend/src/api/media.ts` (new), matching `*.test.tsx`.

## Forbidden
- Any provider-supplied error text on a customer surface; all customer-facing failure
  text must come from the plain-words table in `services/messaging.py`.
- Client-side click recording; clicks are recorded by the backend redirect endpoint only.
- Migrations: `backend/migrations/versions/**` (Fable writes `0030_messaging_completeness`).
- Money and ledger code from P24: `backend/app/models/billing.py`,
  `backend/app/services/credits.py`, `backend/app/services/ai_usage.py`,
  `backend/app/services/stripe_client.py`.
- `backend/app/models/rbac.py` — this phase adds no permission keys.
- `.env`, `deploy/**`, `backend/app/auth/deps.py`.
- No new top-level navigation.

## Test spec
Unit:
- [ ] mms_attachment_rejects_over_size_before_send — a 4 MB image to a provider that
      caps 3.5 MB returns 422 with a plain message.
- [ ] mms_rejected_by_provider_falls_back_to_sms_link — a post-send provider rejection
      creates an SMS with the media URL and marks the failure reason.
- [ ] send_later_rejects_time_inside_contact_quiet_hours — a send scheduled for 21:30 in
      the contact's timezone returns 422 with the plain reason, and no row is written.
- [ ] send_later_is_distinct_from_campaign_schedule — single-message `scheduled_for`
      never appears in campaign dialer queues.
- [ ] short_link_redirect_records_click_once — GET `/l/{code}` increments clicks and
      redirects.
- [ ] link_tracking_disabled_org_does_not_shorten_links — URL is sent verbatim.
- [ ] failure_reason_public_maps_to_plain_word — a known provider code maps to
      “This number can’t receive texts (landline)” and is stored on the message.
- [ ] scheduled_message_cancelled_before_send_leaves_timeline_note — cancellation
      removes the pending send and marks the timeline.
Integration:
- [ ] mms_to_sms_fallback_end_to_end — send an MMS with provider configured to reject
      MMS; the outbox produces an SMS and the timeline shows the fallback.
- [ ] send_later_to_sweeper_to_outbox — schedule a message, advance sweeper time, and
      confirm the message is sent with the correct timezone timestamp.
- [ ] short_link_click_updates_timeline — click a tracked link and see the count on
      the original thread.
Frontend:
- [ ] composer_attachment_tab_shows_size_guard — invalid file is rejected before
      upload.
- [ ] send_later_picker_disables_quiet_hours — forbidden times are greyed out.
- [ ] failure_text_renders_on_bubble — the message bubble shows the public failure
      reason, not a provider code.
Manual: send an actual image MMS to a test number, then view the Clicked count after
opening the generated link on another device.

## Deploy
yes — migration 0030 is additive; no existing send behavior changes unless a user
attaches or schedules.

---

# Phase 29 — Voice completeness (WS-3 + WS-4)

## Goal
Supervisor features hold up, recordings are usable, failover works, dispositions feed
reports, and overnight business hours behave. Human voice calls remain reliable while
the supervisor path becomes enforceable.

## Pre-dependencies
Migration `0031_voice_completeness` (Fable). LiveKit media plane and Telnyx SIP remain
in place.

## Schema (migration 0031, Fable-only)
- `call_recordings.channel_layout` VARCHAR(8) NOT NULL DEFAULT 'mixed'
  (`mixed`|`dual`).
- `orgs.recording_announcement` BOOL NOT NULL DEFAULT false;
  `orgs.recording_announcement_text` TEXT NULL.
- `calls.disposition` VARCHAR(32) NULL; `calls.disposition_note` TEXT NULL.
- Business hours schedule JSON gains overnight support without schema change; engine
  rolls over midnight correctly.

Also fixed while in the area: D17 (activate_flow must re-point numbers) and D35 (voice
event delivery falls back to the database), both assigned to this phase by the outline.
D37 (Bandwidth Numbers API Basic auth) is assigned to P29 by Fable decision (2026-09-10): it
sits in the same provider-account path that voice failover walks.

## Permissions (if any new keys)
No new permission keys. `calls:supervise` remains the gate for supervisor features;
`calls:read`/`calls:place` remain as-is.

## Features (customer-facing names in quotes)
- "Supervisor coaching" — a supervisor can speak privately to the agent mid-call. The
  coaching track is published with client-side subscription permissions so the customer
  leg is not a permitted subscriber, and the backend additionally watches media-plane
  track-subscribed events: if the customer's phone leg is ever seen subscribing to a
  coaching track, the backend removes the track and writes an audit entry. Detect-and-
  enforce, because the server cannot pre-block every subscriber. Closes D15.
- "Dual-channel recording" — LiveKit egress records each participant track separately
  and stitches them at finalize. Download offers "mixed" and "agent/customer" files
  for dual-channel calls.
- "Recording consent announcement" — org toggle plus text played by the flow engine
  before connect on inbound and by the dialer on outbound. Settings shows a plain
  hint list of two-party-consent states labeled “not legal advice”.
- "Voice failover" — dial path implements a CreateCallResult error taxonomy plus
  RoutePlan walk mirroring SMS failover. If one route fails, the next is tried.
  Closes D28.
- "Call result" — after a human call ends, the call card shows a disposition picker
  with a configurable list in Settings → Calling plus a note field. It feeds Reports.
- "Overnight hours" — business hours can span midnight, with correct open/closed
  decisions. Closes D18.
- "Callback queue" — a "Call back" list in the Calls page with one-click dial and a
  timestamp of the missed call.
- Activating a flow now re-points every number assigned to the old flow to the new
  flow. Closes D17.
- Voice event delivery falls back to the database on repeated failure, so a call event is
  never lost when the delivery target is unreachable. Closes D35.
- Bandwidth Numbers API requests use the provider account's Basic auth correctly.
  Closes D37.

## Simplification
- Flows and Queues pages merge into Settings → Calling with one "Call handling"
  wizard: "Answered by: Human ring group / Queue / Assistant / Voicemail". The wizard
  writes the flow. The full node editor remains behind "Advanced".
- No new top-level nav; callback queue lives inside Calls, dispositions inside the
  call card.

## Allowed files (backend and frontend)
Backend: `backend/app/models/voice.py` (calls, call_recordings),
`backend/app/models/org.py` (announcement toggle + text),
`backend/app/models/callflow.py`, `backend/app/services/supervisor.py`,
`backend/app/services/routing_exec.py`, `backend/app/services/dialer.py`,
`backend/app/services/flow_engine.py`, `backend/app/services/flows.py`,
`backend/app/services/recordings.py`, `backend/app/services/webhooks_out.py`,
`backend/app/services/provider_accounts.py` (D37 Basic auth),
`backend/app/api/routes/calls.py`, `backend/app/api/routes/flows.py`,
`backend/app/api/routes/numbers.py`, `backend/app/api/routes/orgs.py`,
`backend/app/api/routes/media.py` (recording download),
`backend/app/api/routes/webhooks.py` (voice event fallback),
`backend/tests/test_p29_*.py`.
Request/response models inline in the route modules (no `schemas` package).
Frontend: `frontend/src/pages/CallsPage.tsx`,
`frontend/src/pages/FlowsPage.tsx` (route removed; node editor mounts behind "Advanced"),
`frontend/src/pages/QueuesPage.tsx` (route removed; content becomes a wizard step),
`frontend/src/pages/SettingsCallingPage.tsx` (new),
`frontend/src/components/calls/DispositionPicker.tsx` (new directory),
`frontend/src/components/calls/CallbackQueue.tsx` (new),
`frontend/src/components/calls/RecordingDownloadDrawer.tsx` (new),
`frontend/src/components/conversations/Timeline.tsx` (call card body),
`frontend/src/components/shell/Sidebar.tsx` (drop Flows and Queues entries),
`frontend/src/api/calls.ts` (new), matching `*.test.tsx`.

## Forbidden
- Supervisor track handling anywhere except `backend/app/services/supervisor.py`.
- Any UI that exposes provider error codes or raw media-plane track details.
- Migrations: `backend/migrations/versions/**` (Fable writes `0031_voice_completeness`).
- Money and ledger code from P24: `backend/app/models/billing.py`,
  `backend/app/services/credits.py`, `backend/app/services/ai_usage.py`,
  `backend/app/services/stripe_client.py`.
- `backend/app/models/rbac.py` — this phase adds no permission keys.
- `.env`, `deploy/**`, `backend/app/auth/deps.py`.
- No new top-level navigation.

## Test spec
Unit:
- [ ] coaching_track_subscription_by_customer_leg_is_removed_and_audited — when the
      customer participant subscribes to the supervisor coaching track, the track is
      removed within one enforcement tick and one `supervisor.coaching_leak` audit entry
      is written. Closes D15.
- [ ] dual_channel_recording_files_offer_mixed_and_separate_downloads — finalized
      recording has both layouts and the endpoint returns all requested files.
- [ ] consent_announcement_played_inbound_and_outbound — the announcement is inserted
      before connect on both paths when enabled.
- [ ] voice_failover_walks_route_plan_on_dial_error — a failed CreateCallResult tries
      the next route and never gives up silently. Closes D28.
- [ ] overnight_hours_span_midnight — a schedule 22:00–06:00 is correctly open at
      02:00 and closed at 12:00. Closes D18.
- [ ] activate_flow_repoints_numbers_to_new_flow — numbers on the old flow point to
      the new flow after activation. Closes D17.
- [ ] voice_webhook_db_fallback_persists_event — delivered webhook failure stores the
      event and marks it for retry. Closes D35.
- [ ] bandwidth_numbers_api_uses_basic_auth — outbound request includes Basic auth
      when required by the provider account. Closes D37.
- [ ] disposition_picker_saves_note_and_feeds_reports — disposition is stored on the
      call and appears in report queries.
- [ ] callback_queue_shows_missed_calls_with_one_click_dial — list contains missed
      calls and triggers the existing dialer.
Integration:
- [ ] inbound_supervisor_call_does_not_leak_coaching_audio — across a full inbound call
      with coaching active, the customer participant's subscribed-track set never
      contains the coaching track.
- [ ] dual_channel_call_recording_stitched_and_downloaded — egress produces two
      tracks, finalize stitches them, and download presents mixed and separate files.
- [ ] failover_to_second_route_dials_once — first route fails, second route succeeds,
      exactly one call is placed.
Frontend:
- [ ] call_card_disposition_picker_lists_configured_options — only admin-configured
      dispositions appear.
- [ ] settings_calling_wizard_writes_flow — selecting "Queue" creates the behind-scenes
      flow without opening the node editor.
- [ ] callback_queue_dial_button_calls_selected_number — button places the call and
      clears the item.
Manual: run one inbound call with supervisor listen-only and confirm no customer
audio leakage; run one outbound call with consent announcement and dual-channel
recording.

## Deploy
yes — migration 0031 is additive; recording layout defaults to 'mixed' and consent
announcement remains off until enabled.

---

# Phase 30 — Reports (WS-7 + WS-9)

## Goal
Four reports a manager actually opens, each exportable and optionally emailed. A
wallboard provides live queue and agent status. Report data includes disposition and
assistant metrics without new write paths.

## Pre-dependencies
Migration `0032_reports` (Fable). Uses P19 spend rollup, P23 assistant tables, P24
usage events.

## Schema (migration 0032, Fable-only)
- `report_schedules` — id, org_id, report VARCHAR(32)
  (`team`|`inbox_sla`|`campaigns`|`assistant`), params JSONB NOT NULL, cadence
  VARCHAR(10) `daily`|`weekly`|`monthly`, recipients JSONB DEFAULT '[]'::jsonb,
  last_sent_at TIMESTAMPTZ NULL, created_by UUID; FK org_id/users; unique
  `(org_id, report, cadence)`.
- `org_email_settings` — org_id PK/FK, provider VARCHAR(10) `smtp`|`postmark`,
  credentials_encrypted TEXT NOT NULL, from_address VARCHAR(255) NOT NULL,
  status VARCHAR(12) NOT NULL DEFAULT 'unverified'; index `(org_id)`.

## Permissions (if any new keys)
No new permission keys. Existing `reports:read` gates Reports and wallboard;
`settings:write` gates report schedules and org email settings.

## Features (customer-facing names in quotes)
- "Reports" page with a date range and four tabs: "Team", "Inbox SLA", "Campaigns",
  "Assistant".
- Team tab: per-agent conversations handled, first-response median, calls made and
  answered, talk time, and dispositions.
- Inbox SLA tab: first response, resolution, breaches, by inbox.
- Campaigns tab: sent, delivered, replied, opted out, and cost read from
  `provider_spend_daily` (P19) and the priced usage rows in `ai_usage_events` (P24).
- Assistant tab: from P23/P24 metrics.
- Every tab supports "Export CSV" (streaming export) and "Email this weekly" when org
  email settings are configured; otherwise a one-time setup card appears.
- "Wallboard" — `/wallboard` full-screen route fed by the existing events stream: calls
  in queue, agents on call, longest waiting, today’s counts. Read-only, reached by direct
  link with a rotating org secret; it is deliberately NOT added to the sidebar.
- `call_scores` get a read endpoint so Assistant and Team tabs can include them.
  Closes D22.
- WS authentication moves from JWT query param to a first-frame token handshake.
  Closes D27.

## Simplification
- Dashboard page becomes the Reports "Today" tab; remove Dashboard from the nav.
- No new top-level nav; Reports is already a Work surface item.

## Allowed files (backend and frontend)
Backend: `backend/app/models/reports.py` (new; report_schedules),
`backend/app/models/org.py` (org_email_settings), `backend/app/services/reports.py` (new),
`backend/app/services/email_settings.py` (new), `backend/app/services/scoring.py`
(call score read path), `backend/app/services/usage.py`, `backend/app/services/spend.py`,
`backend/app/services/analytics.py`, `backend/app/api/routes/reports.py` (new),
`backend/app/api/routes/analytics.py`, `backend/app/api/routes/orgs.py`,
`backend/app/api/routes/calls.py` (score read), `backend/app/events/**` (first-frame
token handshake, D27), `backend/tests/test_p30_*.py`.
Request/response models inline in the route modules (no `schemas` package).
Frontend: `frontend/src/pages/ReportsPage.tsx` (new),
`frontend/src/pages/DashboardPage.tsx` (route removed; content becomes the Reports "Today" tab),
`frontend/src/pages/WallboardPage.tsx` (new; direct-link route, no sidebar entry),
`frontend/src/components/reports/*` (new directory),
`frontend/src/components/shell/Sidebar.tsx` (drop the Dashboard entry),
`frontend/src/api/reports.ts` (new), `frontend/src/api/wallboard.ts` (new),
matching `*.test.tsx`.

## Forbidden
- New write paths for report data; every tab reads existing tables
  (`provider_spend_daily` from P19, `ai_usage_events` and `credit_ledger` from P24,
  `call_scores` and the assistant tables from P23).
- The `call_scores` read endpoint may not expose model hyperparameters or raw
  confidence internals.
- Any new WS connection authenticated by a JWT query parameter; first-frame token only.
- Migrations: `backend/migrations/versions/**` (Fable writes `0032_reports`).
- Money and ledger code from P24: `backend/app/models/billing.py`,
  `backend/app/services/credits.py`, `backend/app/services/ai_usage.py`,
  `backend/app/services/stripe_client.py`.
- `backend/app/models/rbac.py` — this phase adds no permission keys.
- `.env`, `deploy/**`, `backend/app/auth/deps.py`.
- No new top-level navigation.

## Test spec
Unit:
- [ ] team_report_aggregates_conversations_first_response_and_talk_time — per-agent
      rows match fixture data.
- [ ] inbox_sla_report_groups_by_inbox_and_counts_breaches — breach counts use
      `sla_breached_at` from P26.
- [ ] campaign_report_cost_matches_spend_rollup — the campaign cost cell equals the sum
      of `provider_spend_daily` plus `ai_usage_events.price_micros` for the range, to the
      micro.
- [ ] assistant_report_reads_call_scores — call scores are available and included in
      the Assistant tab. Closes D22.
- [ ] report_schedule_validates_cadence_and_recipients — invalid cadence or empty
      recipients returns 422.
- [ ] report_email_requires_verified_org_email_settings — a schedule cannot be created
      without verified settings.
- [ ] ws_auth_uses_first_frame_token_no_query_param — connection URL contains no JWT
      query parameter; token is exchanged in the first frame. Closes D27.
- [ ] wallboard_link_uses_rotating_org_secret — the `/wallboard` URL is tokenless and
      valid only with the current secret.
Integration:
- [ ] report_export_streams_csv_without_memory_blowup — large fixture exports in
      batches and writes to storage, not RAM.
- [ ] email_weekly_report_job_sends_to_recipients — scheduled report triggers email
      via org email settings.
- [ ] dashboard_merged_into_today_tab — today counts render under Reports Today.
Frontend:
- [ ] reports_page_renders_four_tabs — Team, Inbox SLA, Campaigns and Assistant each
      render a skeleton while loading and an error card with a retry button on failure.
- [ ] wallboard_displays_queue_wait_and_agent_status — live WS events update the
      wallboard without page refresh.
- [ ] email_weekly_button_shows_setup_card_when_unverified — one-time setup card
      appears instead of scheduling.
Manual: open Reports, export each tab as CSV, then open `/wallboard` on a separate
screen while a call is queued.

## Deploy
yes — migration 0032 is additive; wallboard and reports are read-only. Requires public
wallboard secret in `.env`.

---

# Phase 31 — Notifications, PWA, and mobile (WS-7)

## Goal
Agents get told about things without keeping the tab open. Notifications land in the
browser, phone, or email; the app is installable and works at 390 px with a mobile
inbox/softphone layout.

## Pre-dependencies
Migration `0033_notifications_pwa` (Fable). Uses P30 email settings for digest.

## Schema (migration 0033, Fable-only)
- `push_subscriptions` — id, org_id, user_id, endpoint TEXT NOT NULL, keys JSONB NOT
  NULL, user_agent TEXT, created_at TIMESTAMPTZ; FK org/user; unique
  `(org_id, user_id, endpoint)`.
- `users.notification_prefs` JSONB NOT NULL DEFAULT '{}'::jsonb — holds the five event
  toggles (`mention`, `assignment`, `new_inbound`, `missed_call`, `sla_breach`) plus the
  daily email digest switch (`digest`). Fable decision: five event toggles plus one daily-digest switch; all six keys stay.

## Permissions (if any new keys)
No new permission keys. Notification preferences are self-service; backend enforces
user-scoped reads/writes.

## Features (customer-facing names in quotes)
- "Browser notifications" — Web Push via VAPID keys in `.env`, user-approved after
  the first relevant event, not on login. Notifications fire for mentions,
  assignments, new inbound in my inboxes, missed calls, and SLA breaches.
- "Install app" — PWA manifest plus service worker for offline shell only. API
  responses are never served from cache.
- "Mobile inbox" — at 390 px, inbox list ↔ detail toggle matches P20 baseline; the
  softphone dock collapses to a bar so the call controls remain reachable.
- "Email digest" — daily summary for leads using P30 email settings; leads may opt out
  per user.
- Notification preferences live in one "Notifications" section in Profile with five
  toggles; no per-inbox matrices.

## Simplification
- One "Notifications" section in Profile holds the five event toggles plus the digest
  switch. No per-inbox or per-channel matrix is ever built — this is the phase that
  forecloses it. What actually goes away: the shell bell's own per-kind settings menu
  from P26 and the standalone browser-permission banner both collapse into this one
  section, and the softphone dock's separate mobile controls disappear into the bar.
- No new top-level nav; the mobile layout reuses the existing Inbox and softphone surfaces.
- Fable decision (2026-09-10): the real removal in this phase is every page-local toast,
  alert, or "new message" banner (inbox, calls, campaigns, softphone dock). They are
  deleted and replaced by the single shell notification centre + Web Push. Test:
  no_page_local_toasts (grep-style test asserts the removed components are gone and
  pages emit through the shared notify() only).

## Allowed files (backend and frontend)
Backend: `backend/app/models/user.py` (`notification_prefs`),
`backend/app/models/push_subscriptions.py` (new),
`backend/app/services/notifications.py` (from P26), `backend/app/services/push.py` (new),
`backend/app/services/digest.py` (new), `backend/app/api/routes/notifications.py`
(from P26; push subscription + preference endpoints added here),
`backend/app/api/routes/orgs.py` (member preference read/write),
`backend/tests/test_p31_*.py`.
Request/response models inline in the route modules (no `schemas` package).
Frontend: `frontend/public/manifest.webmanifest` (new), `frontend/public/sw.js` (new),
`frontend/src/service-worker-register.ts` (new),
`frontend/src/pages/ProfilePage.tsx` (new),
`frontend/src/components/shell/NotificationPreferences.tsx` (new),
`frontend/src/components/shell/BellMenu.tsx` (from P26),
`frontend/src/components/inbox/MobileInboxToggle.tsx` (new),
`frontend/src/pages/InboxPage.tsx` (390 px layout),
`frontend/src/softphone/SoftphonePanel.tsx` (collapse to a bar),
`frontend/src/api/push.ts` (new), matching `*.test.tsx`.

## Forbidden
- Service-worker caching of API responses; the app shell only.
- Any notification permission prompt on login; it may only follow a relevant event.
- Migrations: `backend/migrations/versions/**` (Fable writes `0033_notifications_pwa`).
- Money and ledger code from P24: `backend/app/models/billing.py`,
  `backend/app/services/credits.py`, `backend/app/services/ai_usage.py`,
  `backend/app/services/stripe_client.py`.
- `backend/app/models/rbac.py` — this phase adds no permission keys.
- `.env`, `deploy/**`, `backend/app/auth/deps.py`.
- No new top-level navigation.

## Test spec
Unit:
- [ ] push_subscription_upsert_is_user_scoped — one user cannot replace another
      user’s subscription.
- [ ] notification_prefs_rejects_unknown_keys — a payload containing exactly the five
      event keys plus `digest` is accepted; adding any other key returns 422.
- [ ] push_candidates_fire_only_for_enabled_kinds — a user with `missed_call` off
      gets no push for a missed call.
- [ ] email_digest_uses_p30_email_settings — digest job uses the org email settings
      and skips orgs with unverified settings.
- [ ] digest_user_opt_out_skips_recipient — leads with digest off are not emailed.
- [ ] pwa_manifest_contains_offline_shell_only — no API endpoint patterns in cache
      list.
Integration:
- [ ] mention_triggers_push_and_bell — P26 mention creates a push payload and an
      in-app notification.
- [ ] daily_digest_flow_sends_once_per_user — duplicate digest job does not send
      twice.
Frontend:
- [ ] notification_prompt_shown_after_first_event — prompt UI appears only after a
      relevant event, not automatically on login.
- [ ] mobile_inbox_toggle_at_390px — list/detail switch works at 390 px and preserves
      selected thread.
- [ ] softphone_dock_collapses_to_bar — dock becomes a one-line bar on narrow view.
Manual: subscribe to push, have another user mention you, and confirm the browser
notification appears while the tab is backgrounded.

## Deploy
yes — migration 0033 is additive; VAPID keys required for push, but no-key mode
skips push silently.

---

# Phase 32 — Plans, traffic billing, invoices (WS-9)

## Goal
Charge for everything through the P24 ledger and sell plans. Usage events cover SMS
segments, MMS, voice minutes, number rental, and seats; plan allowances net first,
overage is priced from rate sheet with markup, and invoices are generated monthly.

## Pre-dependencies
Migration `0034_plans_billing` (Fable). P24 ledger and `credit_ledger` exist. Stripe
keys already in `.env` from P24.

## Schema (migration 0034, Fable-only)
- `plans` — code VARCHAR(32) PK, name VARCHAR(120) NOT NULL,
  monthly_price_micros BIGINT NOT NULL, included JSONB NOT NULL DEFAULT '{}'
  (`sms_segments`, `voice_minutes`, `numbers`, `seats`), overage_rates JSONB NOT NULL
  DEFAULT '{}'; all money values in micros.
- `orgs.plan_code` VARCHAR(32) NULL FK plans, `plan_started_at` TIMESTAMPTZ NULL,
  `billing_email` VARCHAR(255) NULL, `tax_id` VARCHAR(64) NULL.
- `invoices` — id, org_id, period_start DATE NOT NULL, period_end DATE NOT NULL,
  status VARCHAR(10) `draft`|`open`|`paid`|`void`, subtotal BIGINT, tax BIGINT, total
  BIGINT, stripe_invoice_id VARCHAR(64) NULL, pdf_key TEXT NULL, created_at;
  billed_to_org_id added later in P33, not here.
- `invoice_lines` — id, invoice_id FK, metric VARCHAR(40), quantity NUMERIC(20,4),
  unit_price BIGINT, amount BIGINT; index `(invoice_id)`.

All money code remains Fable-owned. Implementers may use read-only helpers but never
write to `credit_ledger` outside `services/credits.py`.

## Permissions (if any new keys)
No new permission keys. `org:billing` gates Billing page and plan changes; platform
ops retain `org:billing`-capable access.

## Features (customer-facing names in quotes)
- "Usage billing" — P24 usage events extend to SMS segments, MMS, voice minutes,
  number monthly rental, and seats. Plan allowances are netted first; overage is
  priced from the rate sheet with markup.
- "Prepaid credits" — existing credit balance is used for overage when the org is
  prepaid; plan payments draw from card/Stripe.
- "Monthly invoice" — monthly generation job creates a draft invoice, sends it to
  Stripe or credits drawdown, and stores a PDF key. Billing page shows plan card,
  usage vs allowance bars, and invoices list with PDF download.
- "Hard limits" — numbers cap and seat cap are enforced at order/invite time with a
  plain upgrade message: “Add a plan to invite more people” or “Add a plan to order
  more numbers”.
- Rate sheet customer view shows prices only, never provider costs. Margin is never
  exposed.

## Simplification
- ONE Billing page (the existing P24 Billing page) gains two cards: "Plan" and
  "Invoices". No separate "Plans" navigation.
- No new top-level nav; plan details live inside Settings → Billing & usage.

## Allowed files (backend and frontend)
Backend: `backend/app/models/plans.py` (new),
`backend/app/models/invoices.py` (new), `backend/app/models/org.py` (plan fields),
`backend/app/models/billing.py` (P24 ledger model — read-only helpers only),
`backend/app/models/platform.py` (USAGE_METRICS additions),
`backend/app/models/spend.py`, `backend/app/services/usage.py`,
`backend/app/services/spend.py`, `backend/app/services/plans.py` (new),
`backend/app/services/invoicing.py` (new), `backend/app/services/credits.py`
(Fable-only; every line reviewed), `backend/app/services/stripe_client.py`,
`backend/app/api/routes/billing.py`, `backend/app/api/routes/orgs.py` (plan + seat cap
at invite time), `backend/app/api/routes/numbers.py` (numbers cap at order time),
`backend/app/api/routes/webhooks.py` (Stripe invoice events),
`backend/tests/test_p32_*.py`.
Request/response models inline in the route modules (no `schemas` package).
Frontend: `frontend/src/pages/BillingPage.tsx` (P24), `frontend/src/components/billing/PlanCard.tsx`
(new), `frontend/src/components/billing/UsageAllowanceBar.tsx` (new),
`frontend/src/components/billing/InvoicesList.tsx` (new),
`frontend/src/api/billing.ts` (P24), matching `*.test.tsx`.

## Forbidden
- Migrations: `backend/migrations/versions/**` (Fable writes `0034_plans_billing`).
- Any `credit_ledger` write outside `backend/app/services/credits.py`; ledger rows are
  append-only and are never UPDATEd.
- Customer-facing display of `cost_micros` or margin anywhere in the API or UI.
- Hard-limit enforcement in any route other than number ordering and member invite.
- `backend/app/models/rbac.py` — this phase adds no permission keys.
- `.env`, `deploy/**`, `backend/app/auth/deps.py`.
- No new top-level navigation.

## Test spec
Unit:
- [ ] plan_allowance_nets_before_overage — included SMS segments reduce overage
      quantity to zero if unused.
- [ ] overage_price_uses_rate_sheet_markup — sell price is cost × (1 + markup), never
      cost alone.
- [ ] prepaid_org_overage_draws_from_credits — overage creates usage ledger entries
      against the existing credit balance.
- [ ] monthly_invoice_generation_creates_lines_and_pdf_key — draft invoice has one
      line per metric and a PDF key.
- [ ] invoice_status_transitions_are_valid — draft→open→paid or void, no skipping
      backwards.
- [ ] numbers_cap_blocks_order_when_exceeded — order route returns 422 plain upgrade
      message.
- [ ] seat_cap_blocks_invite_when_exceeded — invite route returns 422 plain upgrade
      message.
- [ ] customer_billing_response_never_includes_cost_micros — rate sheet and API
      response contain only price.
- [ ] credit_ledger_balance_integrity_after_invoice_drawdown — balance after equals
      previous balance minus usage amount.
Integration:
- [ ] monthly_job_to_stripe_invoice_and_credits — org with overage gets Stripe
      invoice or credit drawdown and invoice status open.
- [ ] plan_assignment_updates_hard_limits — after assigning a higher plan, seat invite
      succeeds.
Frontend:
- [ ] billing_page_gains_plan_and_invoices_cards — P24 balance card remains, with two
      new cards on the same page.
- [ ] usage_allowance_bar_shows_remaining — rendered percentage matches allowance.
- [ ] invoices_list_downloads_pdf — download button links to PDF key.
Manual: run monthly invoice job in a sandbox org and verify Stripe invoice, credit
ledger, and PDF download.

## Deploy
yes — migration 0034 is additive; no charges start until a plan is assigned. Stripe
keys must already be set from P24.

---

# Phase 33 — Agencies: sub-accounts and white-label (WS-0 + WS-7 + WS-10)

## Goal
One customer manages many client workspaces under their own brand. Parent admins can
create child workspaces, invite users, view usage, and receive consolidated invoices.
Branding applies logo, colour, app name, support email, sender names, and custom
domain without changing per-tenant data isolation.

## Pre-dependencies
Migration `0035_agencies` (Fable). P32 invoices exist.

## Schema (migration 0035, Fable-only)
- `orgs.parent_org_id` UUID NULL FK orgs ON DELETE SET NULL; index
  `(parent_org_id)`.
- `org_branding` — org_id PK/FK, logo_key TEXT NULL, primary_color VARCHAR(9) NULL,
  app_name VARCHAR(120) NULL, support_email VARCHAR(255) NULL, custom_domain
  VARCHAR(255) NULL, domain_status VARCHAR(10) `pending`|`active`|`failed` NOT NULL
  DEFAULT 'pending'; unique `(custom_domain)`.
- `invoices.billed_to_org_id` UUID NULL FK orgs ON DELETE SET NULL; parent org is
  billed when set. Index `(billed_to_org_id)`.

## Permissions (if any new keys)
No new permission keys. Parent access is an explicit cross-org grant checked in one
service function; `org:billing` on parent authorizes consolidated invoice views.

## Features (customer-facing names in quotes)
- "Client workspaces" — Org switcher groups child workspaces under the parent.
  Parent admins see a "Clients" section in Settings: create workspace, invite members,
  view usage, and see consolidated invoices.
- "Branding" — logo, colour, and app name apply via CSS tokens at load. Sender names
  on emails use the branded app name.
- "Custom domain" — CNAME instructions plus a verification check; TLS is applied via
  an operator runbook step with the shared nginx on the VPS. The API records status
  only.
- Data isolation is unchanged: `TenantScoped` keys still apply per child org. Parent
  access is an explicit cross-org grant checked in one place; no implicit visibility.

## Simplification
- None of this appears for single-workspace customers. Feature-flagged by existence
  of `parent_org_id`.
- No new top-level nav for clients. Parent admin sees one Settings → Clients section.

## Allowed files (backend and frontend)
Backend: `backend/app/models/org.py` (`parent_org_id`),
`backend/app/models/org_branding.py` (new), `backend/app/models/invoices.py`
(`billed_to_org_id`), `backend/app/services/org_access.py` (new; the single cross-org
grant check), `backend/app/services/branding.py` (new),
`backend/app/services/consolidated_invoices.py` (new),
`backend/app/api/routes/orgs.py` (create client workspace, invite, member list),
`backend/app/api/routes/org_branding.py` (new),
`backend/app/api/routes/billing.py` (consolidated invoice views, from P32),
`backend/tests/test_p33_*.py`.
Request/response models inline in the route modules (no `schemas` package).
Frontend: `frontend/src/pages/OrgPickerPage.tsx` (group client workspaces),
`frontend/src/pages/SettingsClientsPage.tsx` (new),
`frontend/src/pages/SettingsBrandingPage.tsx` (new),
`frontend/src/components/branding/BrandingPreview.tsx` (new directory),
`frontend/src/App.tsx` (brand token application at load),
`frontend/src/api/orgs.ts` (new), `frontend/src/api/branding.ts` (new),
matching `*.test.tsx`.

## Forbidden
- Any parent access that bypasses `TenantScoped` or reaches a sibling workspace;
  cross-org reach exists only through `services/org_access.py`.
- TLS or nginx changes for custom domains — the API records status only; certificate
  issuance is an operator runbook step.
- Migrations: `backend/migrations/versions/**` (Fable writes `0035_agencies`).
- Money and ledger code from P24: `backend/app/models/billing.py`,
  `backend/app/services/credits.py`, `backend/app/services/ai_usage.py`,
  `backend/app/services/stripe_client.py`.
- `backend/app/models/rbac.py` — this phase adds no permission keys.
- `.env`, `deploy/**`, `backend/app/auth/deps.py`.
- No new top-level navigation.

## Test spec
Unit:
- [ ] child_org_inherits_tenant_scope_in_all_queries — list routes are scoped to the
      child org even when parent admin is authenticated.
- [ ] parent_cross_org_grant_is_explicit_and_checked — parent admin can access child
      resources only through the explicit grant function.
- [ ] sibling_workspace_isolation_preserved — parent admin cannot access a child org
      not granted to them.
- [ ] branding_css_tokens_returned_at_load — API returns logo/colour/app_name for the
      child org.
- [ ] custom_domain_status_requires_verification — setting a custom domain without
      prior verification leaves status pending.
- [ ] consolidated_invoice_billed_to_parent — an invoice with `billed_to_org_id` is
      excluded from the child org’s billing page.
- [ ] single_workspace_feature_flag_hides_all_agency_ui — parent_org_id NULL means no
      Clients section or branding fields.
Integration:
- [ ] create_client_workspace_invite_and_invoice — parent creates child, invites user,
      child usage rolls into consolidated invoice.
- [ ] branding_flows_to_login_and_emails — child org’s app_name appears in email
      sender name and CSS tokens.
Frontend:
- [ ] org_picker_groups_client_workspaces — child workspaces appear grouped under
      parent, not flat.
- [ ] clients_page_lists_usage_and_invoices — parent sees per-child usage and
      consolidated invoice link.
- [ ] branding_preview_updates_tokens — colour/logo preview updates without full
      reload.
Manual: as a parent admin, create a child workspace, invite a new user, switch into
the child, confirm child data does not see parent data, then view consolidated
invoice.

## Deploy
yes — migration 0035 is additive; no single-workspace customer sees any change.

---

# Phase 34 — Developers and integrations (WS-9)

## Goal
Connect the tools customers already use with one card per integration. Public API docs
are available, Zapier has simple actions, HubSpot/Salesforce sync contacts, and generic
webhooks get a friendlier UI.

## Pre-dependencies
Migration `0036_integrations` (Fable). Existing API keys and outbound webhooks from
P13.

## Schema (migration 0036, Fable-only)
- `integrations` — id, org_id, kind VARCHAR(20)
  (`hubspot`|`salesforce`|`zapier`|`generic_webhook`), credentials_encrypted TEXT NULL,
  config JSONB NOT NULL DEFAULT '{}', status VARCHAR(12) NOT NULL DEFAULT
  'unverified', last_sync_at TIMESTAMPTZ NULL, last_error TEXT NULL; index
  `(org_id, kind)`.
- `integration_sync_log` — id, integration_id FK, at TIMESTAMPTZ, direction
  VARCHAR(6) `in`|`out`, entity VARCHAR(20), state VARCHAR(12) `ok`|`error`,
  detail TEXT NULL; index `(integration_id, at)`.

## Permissions (if any new keys)
No new permission keys. Existing `settings:write` gates integration creation and
disconnect; API key permissions remain as P13.

## Features (customer-facing names in quotes)
- "Public API docs" — public OpenAPI JSON plus a Docs page served by the SPA.
  Closes D23.
- "Zapier actions" — the existing outbound event delivery already covers triggers;
  publish a trigger/action catalogue plus an "Actions" set (send text, create contact,
  start call) behind API keys.
- "Custom connection" — the existing outbound event delivery gets a friendlier card:
  target address, which events to send, a status pill, and the last few delivery results.
  No new transport is built.
- "HubSpot sync" and "Salesforce sync" — OAuth connect, contact sync both ways using
  phone as join key, log SMS/calls as activities on the contact. Conflict rule:
  CSaaS wins for phone; CRM wins for name/email.
- "Developer settings" — Platform page splits: developer bits move to Settings →
  Developers; integrations move to Settings → Integrations as one card each.

## Simplification
- Platform page splits; developer settings and integrations are no longer mixed.
- No new top-level nav; both live under Settings.

## Allowed files (backend and frontend)
Backend: `backend/app/models/integrations.py` (new),
`backend/app/services/integrations.py` (new), `backend/app/services/apikeys.py`,
`backend/app/services/webhooks_out.py`, `backend/app/services/contacts.py` (sync join),
`backend/app/api/routes/integrations.py` (new),
`backend/app/api/routes/developers.py` (new; API keys, event delivery, OpenAPI JSON),
`backend/app/api/routes/platform.py` (ops-only content stays), `backend/app/main.py`
(public OpenAPI mount), `backend/tests/test_p34_*.py`.
Request/response models inline in the route modules (no `schemas` package).
Frontend: `frontend/src/pages/PlatformPage.tsx` (platform-ops content only),
`frontend/src/pages/SettingsDevelopersPage.tsx` (new),
`frontend/src/pages/SettingsIntegrationsPage.tsx` (new),
`frontend/src/pages/DocsPage.tsx` (new; unauthenticated),
`frontend/src/api/integrations.ts` (new), `frontend/src/api/developers.ts` (new),
matching `*.test.tsx`.

## Forbidden
- Storing customer CRM OAuth tokens unencrypted; reuse the P17 encryption helper.
- Any new outbound transport; integrations reuse the existing event delivery service.
- The words "webhook", "endpoint", or a provider error code on a customer surface;
  use "Custom connection", "Developer settings", "Integration".
- Migrations: `backend/migrations/versions/**` (Fable writes `0036_integrations`).
- Money and ledger code from P24: `backend/app/models/billing.py`,
  `backend/app/services/credits.py`, `backend/app/services/ai_usage.py`,
  `backend/app/services/stripe_client.py`.
- `backend/app/models/rbac.py` — this phase adds no permission keys.
- `.env`, `deploy/**`, `backend/app/auth/deps.py`.
- No new top-level navigation.

## Test spec
Unit:
- [ ] public_openapi_json_served_unauthenticated — `GET /openapi.json` with no
      credentials returns 200 and a document whose `paths` include the customer Actions
      endpoints. Closes D23.
- [ ] zapier_actions_catalogue_is_public — catalogue lists allowed actions with
      required parameters.
- [ ] zapier_action_endpoints_require_api_key — invalid or missing key returns 401.
- [ ] hubspot_oauth_connect_stores_encrypted_credentials — tokens are encrypted and
      never echoed.
- [ ] contact_sync_uses_phone_join_key — same phone matches existing contact before
      creating one.
- [ ] conflict_rule_phone_wins_for_phone_email_for_name — inbound CRM update cannot
      overwrite CSaaS phone; name/email may update.
- [ ] sync_failure_logs_row_and_status — a rejected sync writes `integration_sync_log`
      and updates `last_error`.
- [ ] custom_connection_reuses_existing_delivery — creating a "Custom connection"
      writes one `integrations` row of kind `generic_webhook` and one row in the existing
      outbound delivery table; no second transport is instantiated.
Integration:
- [ ] hubspot_sync_inbound_updates_name_and_email_but_not_phone — CRM name/email flow
      in, phone is kept.
- [ ] zapier_send_text_flow — a Zapier action with API key sends an SMS through the
      inbox/outbox pipeline.
Frontend:
- [ ] platform_page_split_into_developers_and_integrations — two separate Settings
      sections, no mixed cards.
- [ ] docs_page_renders_openapi_without_auth — public docs load without login.
- [ ] integration_card_shows_status_and_last_sync — card has status pill and last
      sync time.
Manual: connect a demo HubSpot account, sync one contact, then verify SMS/call is
logged as an activity and phone conflicts are preserved.

## Deploy
yes — migration 0036 is additive; existing API keys and webhooks continue unchanged.
Public OpenAPI is read-only.

---

# Phase 35 — Channels: email, WhatsApp, web chat (WS-2 + WS-7)

## Goal
The same inbox handles three more channels with zero new pages. Email, WhatsApp, and
web chat threads appear in the existing three-pane inbox; channel-specific composer
requirements are small and surface as a small icon.

## Pre-dependencies
Migration `0037_channels` (Fable). Existing messaging/media services handle SMS and
call threads.

## Schema (migration 0037, Fable-only)
- `message_threads.channel` VARCHAR(10) NOT NULL DEFAULT 'sms'
  (`sms`|`call`|`email`|`whatsapp`|`webchat`). Backfill existing rows from existing
  data; default for new rows is determined by the creating channel.
- `channel_accounts` — id, org_id, kind VARCHAR(10) (`email`|`whatsapp`|`webchat`),
  credentials_encrypted TEXT, config JSONB NOT NULL DEFAULT '{}', status VARCHAR(12)
  NOT NULL DEFAULT 'unverified'; unique `(org_id, kind)`.
- `messages.subject` VARCHAR(255) NULL; `messages.html_body` TEXT NULL — email only.
- `webchat_widgets` — id, org_id, key VARCHAR(64) UNIQUE, allowed_origins JSONB NOT
  NULL DEFAULT '[]', theme JSONB NOT NULL DEFAULT '{}'.

## Permissions (if any new keys)
No new permission keys. Channel account setup uses `settings:write`; agent interaction
uses existing inbox permissions.

## Features (customer-facing names in quotes)
- "Email" — inbound by forwarding address or IMAP poll; threads by Message-Id and
  References. Composer shows a subject field for email.
- "WhatsApp" — Meta Cloud API or Twilio. Templates plus the 24-hour session rule are
  enforced with a plain explanation when an outbound WhatsApp message is not allowed.
- "Web chat" — embeddable script tag points to a widget; visitor identity is cookie
  plus optional email. Agent replies in the same inbox.
- "Channel icon" — each thread row shows a small icon for SMS, call, email, WhatsApp,
  or web chat.
- Compliance: opt-out keywords apply to SMS and WhatsApp only; email gets an
  unsubscribe link and cannot be sent after unsubscribe.

## Simplification
- No new top-level pages. Channel setup is one Settings → Channels section with three
  cards: Email, WhatsApp, Web chat.
- The composer adapts in place: subject field appears only for email; template rules
  appear only for WhatsApp.

## Allowed files (backend and frontend)
Backend: `backend/app/models/messaging.py` (`channel`, `subject`,
`html_body`), `backend/app/models/channel_accounts.py` (new),
`backend/app/models/webchat_widgets.py` (new), `backend/app/services/channels.py` (new),
`backend/app/services/messaging.py` (opt-out keyword scope, unsubscribe link),
`backend/app/services/media.py`, `backend/app/api/routes/channels.py` (new; includes the
unauthenticated widget and email-inbound endpoints),
`backend/app/api/routes/conversations.py`, `backend/app/api/routes/inbox.py`,
`backend/app/api/routes/messages.py`, `backend/app/api/routes/compliance.py`,
`backend/tests/test_p35_*.py`.
Request/response models inline in the route modules (no `schemas` package).
Frontend: `frontend/src/pages/SettingsChannelsPage.tsx` (new),
`frontend/src/components/inbox/Composer.tsx`,
`frontend/src/components/inbox/ThreadList.tsx` (channel icon),
`frontend/src/components/inbox/ChannelIcon.tsx` (new),
`frontend/src/components/inbox/SubjectField.tsx` (new),
`frontend/src/api/channels.ts` (new), `frontend/src/api/conversations.ts`,
matching `*.test.tsx`.

## Forbidden
- New top-level navigation for channels; channel is a thread-row icon and one
  Settings section.
- Opt-out keyword handling for email; email unsubscribes by link only.
- Pricing or usage-metering changes for the new channels; that stays with P32.
- Migrations: `backend/migrations/versions/**` (Fable writes `0037_channels`).
- Money and ledger code from P24: `backend/app/models/billing.py`,
  `backend/app/services/credits.py`, `backend/app/services/ai_usage.py`,
  `backend/app/services/stripe_client.py`.
- `backend/app/models/rbac.py` — this phase adds no permission keys.
- `.env`, `deploy/**`, `backend/app/auth/deps.py`.
- No new top-level navigation.

## Test spec
Unit:
- [ ] email_inbound_builds_thread_by_message_id — repeated emails thread together
      using Message-Id and References.
- [ ] whatsapp_outside_24h_session_rejected_with_plain_explanation — a free-form
      WhatsApp message after the session window returns a plain error.
- [ ] webchat_visitor_identity_cookie_and_optional_email — same cookie reuses thread;
      optional email merges subsequent messages.
- [ ] channel_icon_maps_each_channel — thread list API returns a channel value that
      maps to the correct icon.
- [ ] email_composer_subject_required — sending email without subject returns 422.
- [ ] opt_out_keywords_apply_to_sms_whatsapp_only — email does not opt out via
      keyword; only unsubscribe link does.
Integration:
- [ ] email_inbound_to_inbox_to_agent_reply — a forwarded email creates a thread,
      agent replies, and message contains subject + body.
- [ ] webchat_script_to_thread_to_agent — an embed widget message creates a
      `webchat` thread in the assigned inbox.
- [ ] whatsapp_template_response_flow — templated WhatsApp response sends after
      template match.
Frontend:
- [ ] settings_channels_three_cards — every channel has its own card with status.
- [ ] composer_subject_field_only_for_email — subject field does not render for SMS.
- [ ] thread_row_shows_channel_icon — different channels render distinct icons.
Manual: create an email forwarding address, send a test email, reply from the inbox,
and confirm threading; then test web chat with two separate browser cookies.

## Deploy
yes — migration 0037 is additive; all existing threads default to `sms` and no
channel is enabled until configured.

---

# Phase 36 — Trust: PII encryption at rest, status history, i18n (WS-0 + WS-10)

## Goal
Pass the security questionnaire. Encrypt PII at rest, provide a public status page,
ship English and Spanish, and export SOC2 evidence. This phase must not change any
customer workflow.

## Pre-dependencies
Migration `0038_pii_status_i18n` (Fable). `CREDENTIALS_MASTER_KEY` already exists
from P17.

## Schema (migration 0038, Fable-only)
- `messages.body_enc` TEXT NULL — encrypted envelope for `messages.body`.
- `call_transcripts.text_enc` TEXT NULL — encrypted envelope for transcript text.
- `contact_notes.body_enc` TEXT NULL — encrypted envelope for contact note bodies.
- Envelope encryption uses per-org data keys wrapped by `CREDENTIALS_MASTER_KEY`; key
  rotation job rotates data keys without downtime.
- Plaintext columns are NOT dropped in 0038; a follow-up migration drops them after
  backfill is verified.
- Search moves to a hashed-token index for exact phone/name match plus per-org
  encrypted index for name prefix. Full-text search on message bodies is dropped.
  This trade-off is stated plainly in Settings.
- `status_incidents` — id, started_at TIMESTAMPTZ NOT NULL, resolved_at TIMESTAMPTZ
  NULL, component VARCHAR(40), severity VARCHAR(10) (`low`|`medium`|`high`|
  `critical`), title TEXT, body TEXT, created_at; index `(component, started_at)`.

## Permissions (if any new keys)
No new permission keys. Status incidents are written via platform ops token only;
public status page is read-only.

## Features (customer-facing names in quotes)
- "Encrypted data at rest" — transparent encryption/decryption in the ORM layer via
  `TypeDecorator`, so routes do not change. Data appears plaintext to authorized API
  responses but is encrypted in the database.
- "Search trade-off" — exact phone/name match plus name prefix search remain; full
  message-body search is removed. Settings shows this in plain language.
- "Status page" — public status page with 90-day history and incidents. Operations
  writes incidents via the platform token.
- "Language" — frontend string extraction with react-i18next; English and Spanish are
  shipped. Date/number locale follows the browser.
- "SOC2 evidence" — quarterly access-review export (members, roles, last login) and
  audit-log CSV from the audit history.

## Simplification
- None visible. This phase must not change any customer workflow.
- No new top-level navigation; Settings gains only language and data/search copy.

## Allowed files (backend and frontend)
Backend: `backend/app/models/messaging.py` (`body_enc`),
`backend/app/models/voice.py` (call_transcripts `text_enc`),
`backend/app/models/contacts.py` (contact_notes `body_enc`),
`backend/app/models/status_incidents.py` (new),
`backend/app/services/encryption.py` (new; the `TypeDecorator` and the per-org data-key
wrap/unwrap), `backend/app/services/search.py`, `backend/app/services/audit.py`,
`backend/app/services/access_review.py` (new),
`backend/app/api/routes/status.py` (exists; gains public incident history),
`backend/app/api/routes/platform.py` (ops incident writes),
`backend/tests/test_p36_*.py`.
Request/response models inline in the route modules (no `schemas` package).
Frontend: `frontend/src/i18n/index.ts` (new), `frontend/src/i18n/en.json` (new),
`frontend/src/i18n/es.json` (new), all existing `frontend/src/pages/*.tsx` and
`frontend/src/components/**/*.tsx` for string extraction only (no behaviour change),
`frontend/src/pages/SettingsWorkspaceDataPage.tsx` (P27; search trade-off copy),
`frontend/src/pages/StatusPage.tsx` (new; unauthenticated),
`frontend/src/api/status.ts` (new), matching `*.test.tsx`.

## Forbidden
- Dropping any plaintext column in 0038; that is a follow-up migration after the
  backfill is verified.
- Changing the existing encrypt/decrypt contract in `backend/app/services/credentials.py`.
- Any customer-visible workflow change; string extraction must not alter behaviour.
- Migrations: `backend/migrations/versions/**` (Fable writes `0038_pii_status_i18n`).
- Money and ledger code from P24: `backend/app/models/billing.py`,
  `backend/app/services/credits.py`, `backend/app/services/ai_usage.py`,
  `backend/app/services/stripe_client.py`.
- `backend/app/models/rbac.py` — this phase adds no permission keys.
- `.env`, `deploy/**`, `backend/app/auth/deps.py`.
- No new top-level navigation.

## Test spec
Unit:
- [ ] encrypting_type_decorator_encrypts_on_write — inserted body is stored in
      `body_enc` and not in plaintext.
- [ ] decrypting_type_decorator_decrypts_on_read — API route returns original
      plaintext via ORM layer.
- [ ] per_org_data_key_rotation_updates_wrapped_keys — rotation produces a new data
      key wrapped by `CREDENTIALS_MASTER_KEY` without changing plaintext.
- [ ] search_exact_phone_and_name_still_works — exact phone/name lookup finds rows;
      name prefix works.
- [ ] full_text_body_search_is_removed — full-text query on message bodies returns
      not implemented or an explicit unsupported response.
- [ ] status_incidents_are_platform_ops_only — a normal org admin cannot create an
      incident.
- [ ] public_status_returns_90_day_history — read-only status endpoint returns
      incidents from the last 90 days.
- [ ] access_review_export_contains_members_roles_last_login — export includes the
      four required columns.
- [ ] audit_log_csv_exports_all_events — CSV contains every audit entry for the org,
      ordered newest first.
Integration:
- [ ] encryption_and_search_flow — a message written before encryption is backfilled,
      exact search finds it, and body search is no longer available.
- [ ] incident_to_public_status_flow — platform ops creates an incident, public page
      renders it, and 90-day history appears.
Frontend:
- [ ] settings_workspace_data_page_shows_search_trade_off — the full-text removal is stated in
      plain language.
- [ ] language_switch_english_spanish — switching changes strings and date/number
      formats.
- [ ] status_page_renders_incidents_without_auth — no login required.
Manual: run the key rotation job against a sandbox org and confirm API reads remain
correct; then access `/status` in a logged-out browser.

## Deploy
yes — migration 0038 is additive; no customer workflow changes. Requires
`CREDENTIALS_MASTER_KEY` and `PLATFORM_OPS_TOKEN` to be present.