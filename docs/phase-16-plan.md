# Phase 16 — Quo-style Interface (three-pane inbox, unified call+SMS timeline)

## Goal
Rebuild the console around the inbox, modeled on the user's Quo screenshot: dark
three-pane layout where each number is an inbox in the sidebar, a conversation shows
SMS **and** call events (missed call, outbound call, voicemail + transcript, failed
call with reason) in ONE timeline per contact, with a contact panel on the right.
Plus the P15 management UI (departments, inbox grants) so tiered access is usable
without curl. This kills the "calls live on a separate page" UX the user rejected.

## Pre-dependencies
None (P15 deployed; APIs live: GET /api/v1/inboxes, /departments, grants endpoints).

## Backend support (small, build first — vertical slice)
NEW routes (backend/app/api/routes/conversations.py, registered in main.py):
- `GET /api/v1/conversations?inbox_id=&tab=chats|calls&filter=open|unread|unresponded&q=`
  → conversation rows for the middle pane: contact (or bare number), snippet of the
  LAST event ("Missed call", "You: <text>", "Called you", "Voicemail: <transcript…>"),
  timestamp, unread flag. Merges MessageThread + Call history per (our_e164,
  contact_e164) pair. P15-gated via resolve_access (member+viewer see; reuse the
  existing pattern). "calls" tab = pairs whose last event is a call.
- `GET /api/v1/conversations/{contact_e164}/timeline?our_e164=&cursor=`
  → chronological merged events: message bubbles (with status ticks), call cards
  {direction, status, duration, failure detail (surface bandwidth_create_call_rejected
  detail — e.g. the trial 402 — from CallLeg/VoiceEvent), recording refs, voicemail +
  transcript}. Cursor pagination, newest page first.
Allowed backend files: the new conversations.py, main.py (router line), plus READ-ONLY
use of existing services. FORBIDDEN: models/, migrations/, everything else backend.

## Frontend rework (frontend/src/** is the implementer's scope)
Layout (match screenshot proportions; dark theme default):
1. **Sidebar** (left, narrow): workspace name; Search, Activity, Contacts, Analytics,
   Settings nav; **Inboxes** section from GET /api/v1/inboxes — name + number, color
   dot, admin sees all (grouped by department where granted that way); active inbox
   highlighted. Softphone dock stays global.
2. **Middle pane**: tabs Chats | Calls (Tasks tab NOT in P16 — no tasks model; do not
   render a dead tab); filter chips Open ▾ / Unread / Unresponded / search; conversation
   rows (avatar/initials, name-or-number, snippet incl. call events, date).
3. **Main pane**: header (contact name/number + call, message, more actions); unified
   timeline (SMS bubbles left/right + call event cards inline, incl. red "Call failed —
   <carrier detail>" so a Bandwidth 402 is visible in-thread); composer at bottom
   (attach, template, emoji, schedule icons may be stubs where the feature exists
   elsewhere — no dead buttons for features that don't exist).
4. **Contact panel** (right): avatar, name edit, call/message buttons, Contact fields
   (company, role, phone, email, address from the contacts API), Notes; "shared with"
   line showing which departments/users hold grants (admin only).
5. **Settings → Inboxes & Departments** (the P15 admin UI): departments CRUD +
   member picker; per-inbox grant editor (grant to department or user, member/viewer);
   number → inbox naming/color.
6. Keep: existing pages (Calls history, Campaigns, Numbers, etc.) reachable from
   sidebar Settings/More — nothing deleted, the inbox becomes home.

## Guardrails
- Implementer may touch: frontend/src/** (create/edit freely), the two named backend
  files, and frontend tests. Nothing else. No new runtime deps without Fable approval
  (Tailwind/lucide/React Query already present and sufficient).
- The P15 access model is LAW: the UI must never show an inbox, conversation, or event
  the APIs wouldn't return; no client-side "admin" inference beyond what /inboxes says.

## Test Spec
Backend:
- [ ] conversations list: merge + snippet correctness for each last-event type; tab=calls
      filter; unread flag from last_read_at; P15 gating (ungranted inbox_id → 404/empty)
- [ ] timeline: strict chronological merge across messages/calls/voicemails; cursor
      pagination stable across a live insert; failure detail surfaced on failed legs
Frontend (vitest + existing patterns):
- [ ] sidebar renders inboxes from API, admin grouping, active state
- [ ] conversation list renders snippets for message + missed-call + voicemail rows
- [ ] timeline renders bubble vs call-card vs failed-call-card correctly
- [ ] grant editor: grant to dept vs user, member vs viewer round-trips
E2E smoke (existing frontend test approach):
- [ ] login → pick inbox → open conversation → see call event + send message

Pass criteria: ALL new tests + entire existing backend (930) and frontend (66) suites
green. Then Opus review (UI + the two backend files), Fable sign-off, deploy.

## Deploy
yes (deploy.sh; frontend build ships with it).
