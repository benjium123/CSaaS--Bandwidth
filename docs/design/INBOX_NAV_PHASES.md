# Inbox, settings and team redesign: phases

Spec: `INBOX_NAV_SPEC.md`. Branch `feat/inbox-nav-redesign`, worktree `C:/Users/omer_/csaas_nav` (from origin/main cfed482; latest migration 0091).
Every phase is a vertical slice: shippable alone, tests green, deployed only when its flag says yes.

## Phase 1: Shell, top bar, status pill, settings menu (UI only)
Goal: new top bar and settings menu; inbox sidebar keeps today's data (flat departments) but gets the new layout, folds, compact rows, collapse button and phone dock.
Files: `components/shell/*` (new TopBar, StatusPill), `components/conversations/InboxColumn.tsx`, new `PhoneDock.tsx`, `pages/SettingsPage.tsx`, `pages/settingsSections.ts`, `App.tsx` (redirects for removed Developers/Providers), `call_prefs` `dnd_until` read/write in the existing member-prefs route.
Tests: nav gating (existing `navGating.verify.test.tsx` updated), status pill sets DND with each duration and expires, dock hidden on Settings, Developers/Providers absent for customers but routes redirect, sidebar never scrolls at 720 px with 7 lines, setup card hides when done.
Deploy: yes.

## Phase 2: Lines + Departments pages, carrier/cost hidden, 10DLC cap
Goal: Lines list + focused line page + Departments cards replace Phone numbers and Departments & inboxes; one access editor.
Files: new `pages/LinesPage.tsx`, `pages/LineDetailPage.tsx`, `pages/DepartmentsPage.tsx`; reuse `PortingPanel`, `EmergencyAddressForm`, grants editor from `NumberAccessDrawer` (single component, three entry points); backend: campaign assignment refuses the 51st number (`compliance/registration.py` or the assign route) + `used/cap` in the campaigns API; customer number serializers stop returning provider and cost (ops console keeps them).
Tests: every capability in the capability table (order, add, port, release, port lock, answered-by, campaign, 911, SLA targets, grants) reachable from the new pages; 51st assignment → 409; customer API responses contain no provider/cost fields for owner, admin, member; ops console still shows them.
Deploy: yes.

## Phase 3: Reporting lines, line owners, sub-teams (security-critical)
Goal: `reports_to` + line owner + team name; subtree access; Team page; org-shaped inbox sidebar; "Needs a spot".
Migration `0092_reporting_lines` (renumber if main moves): `org_memberships.reports_to_user_id` (nullable FK, same org), `org_memberships.team_name` (nullable), `inboxes.owner_user_id` (nullable FK). No backfill beyond NULL.
Backend: subtree rule added in ONE place, `services/inbox_access.py`, and every caller audited: `contact_visibility.py`, `inbox_sla.py`, `notifications.py` (missed calls to managers via bell, never ringing), `routes/inboxes.py`, `routes/departments.py`, `routes/orgs.py`, `routes/softphone.py` `_event_visible`, conversations/calls list endpoints. Cycle check on write; leaver's lines move to their manager.
Frontend: Team page (tree, drag, ··· menu, Undo, Needs a spot), sidebar tree (My lines / Shared / Departments or My team).
Tests: per caller above, a rep cannot see a peer's line (texts, calls, live events, notifications, contacts); a manager sees the whole subtree, 3 levels deep; moving a person moves access; loop refused; leaver hand-off; admin scope unchanged; existing grant tests still pass. Opus review of the access diff before merge (R5).
Deploy: yes, after review.

## Order and gates
1 → 2 → 3. Each phase: tests green → commit → user OK → deploy. Phase 3 does not start until Phase 2 is live and verified.
