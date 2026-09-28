# Phase 1: Shell, top bar, status pill, resizable columns, sidebar layout, settings menu

Spec: `INBOX_NAV_SPEC.md` §1-3. Mockup: `inbox-settings-final.html` (v10). Branch `feat/inbox-nav-redesign`.
Split into three steps; each ends green and committed.

## Step 1a: Top bar, status pill (timed DND), resizable columns
Allowed files:
- NEW `frontend/src/components/shell/TopBar.tsx`, `StatusPill.tsx`, `useColumnWidths.ts`, `ColumnSplitter.tsx` (+ tests)
- `frontend/src/App.tsx` (Shell: TopBar above `main` on desktop; icon rail no longer rendered on desktop; MobileTabBar unchanged)
- `frontend/src/pages/ConversationsPage.tsx` (grid widths from `useColumnWidths`, splitters between inbox column | list | thread)
- `frontend/src/softphone/SoftphonePanel.tsx` (DND checkbox reads/writes the same prefs; shows time left)
- `backend/app/services/call_prefs.py` (`dnd_until` ISO string; DND effective only while `now < dnd_until` or `dnd_until` null), `backend/app/api/routes/me.py` (accept/return `dnd_until`) + backend test
Forbidden: migrations, anything under `backend/app/services/inbox_access.py`, billing, auth.
Notes:
- TopBar nav = `useRailNav()` items (same permission gating) + Settings when `canSeeSettings`; Inbox shows total unread from `["inbox-unread-counts"]`. Search opens `openCommandPalette()`. Bell = existing `NotificationBell`, help = existing `HelpMenu`, avatar menu = workspace switch + theme + sign out (moved from InboxColumn footer/rail).
- StatusPill: Available / DND 30m, 1h, until 8:00 AM (org time zone), until turned off; "While on DND, calls go to" = existing `forward_to` teammates. Label shows time left.
- Columns: inbox column 200-380 px (default 256), list 260-520 (default 320). Drag, double-click or button to close/open; widths in localStorage `ringlite.inbox.widths`; existing `ringlite.inbox.rail-collapsed` keeps working. Keyboard: splitter is a `role="separator"` with arrow keys.
- Keep every accessible name listed in the fact sheet that tests use ("Inbox column", "Toggle contact panel", "Do not disturb", "Send my calls to", line row = inbox.name).
Tests:
- [ ] StatusPill: each duration PUTs `{dnd:true, dnd_until}`; Available PUTs `{dnd:false}`; time-left label; forward select only while DND
- [ ] backend: expired `dnd_until` → `get()` returns dnd false and `dnd_users` excludes the user; null until = indefinite
- [ ] TopBar: nav gated exactly like the old rail (reuse `navGating.verify` cases); palette opens; bell + help render
- [ ] useColumnWidths: clamps, persists, restores, close/open
- [ ] Existing suites green: shell, conversations, inbox, softphone, settings (update only assertions about the removed desktop rail, never weaken a permission assertion)

## Step 1b: Inbox sidebar layout
Allowed: `components/conversations/InboxColumn.tsx`, NEW `PhoneDock.tsx`, `NeedsYouChips.tsx` (+ tests), `pages/ConversationsPage.tsx`.
New Message + collapse button on top; needs-you chips (missed calls, waiting on reply, voicemails, assigned: counts from existing conversation filters; hide a chip whose query does not exist yet and note it); All inboxes; lines grouped in department folds (multi-open, remembered, closed fold shows unread); compact rows over 6 visible lines; phone dock (dial, From picker, live-call controls) at the bottom; workspace nav/footer removed (now in top bar). Line row name, unread label, reorder and access button keep their accessible names.
Tests: folds + unread totals, compact threshold, dock dials via existing softphone API, no vertical overflow at 720 px with 7 lines (jsdom layout check on the scroll container class).

## Step 1c: Settings menu
Allowed: `pages/SettingsPage.tsx`, `pages/settingsSections.ts` (+ tests), `App.tsx` redirects.
Groups Phone system / Workspace / Your account; search box (client filter); setup card from the existing setup checklist (hidden when fully set up); sub-pages of the open section in the menu (reuse `?tab=` ids); flags; Developers and Providers removed from customer menu (routes redirect to /settings/workspace; ops console unaffected). "Notifications & sound" section: desktop alerts, ringtone, audio device test (reuses existing ringtone/push settings if present, else a placeholder card, noted).
Tests: gating unchanged per section (`canViewSettingsSection`), search filters, sub-page links set `?tab=`, removed sections redirect, member sees only My profile + Notifications & sound.

## Deploy
After 1a-1c green and the user has clicked through a local build: yes.
