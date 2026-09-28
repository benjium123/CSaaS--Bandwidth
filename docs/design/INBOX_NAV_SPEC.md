# Inbox, settings and team redesign: spec

Approved design: `docs/design/inbox-settings-final.html` (artifact https://claude.ai/artifact/StGdU4tmhpxDGrfiNvt95m, v9).
Decisions from the user, 2026-09-28/29.

## 1. App shell
- **Top bar** (full width, 52 px): logo, main nav (Inbox, Contacts, Campaigns, Analytics, Settings; Inbox shows the unread total), search (Ctrl K, existing command palette), live-call pill (while on a call), **status pill**, bell (existing `NotificationBell`), help (existing `HelpMenu`), avatar menu.
- **Status pill** = Available / Do not disturb with time left. Menu: Available; DND for 30 min / 1 h / until 8:00 AM / until I turn it off; "While on DND, calls go to" (voicemail or a teammate: existing per-member DND + forward, `org_memberships.call_prefs`). The softphone's DND checkbox reads/writes the same state. DND with an end time needs `dnd_until` in `call_prefs` (JSON, no migration).
- Must not copy OpenPhone/Quo names, branding or icons.

## 2. Inbox sidebar (256 px, never scrolls)
Top to bottom: New message + collapse icon button (Ctrl [); "needs you" chips (missed calls, waiting on your reply, voicemails, assigned to you; click filters the list); **Inbox**: All inboxes, then the org-shaped tree; **phone dock** at the bottom.
- Tree for the viewer: **My lines**, **Shared with <department>** (department-shared lines I don't own), then **Departments** (workspace owner: one fold per direct report, labelled with that report's department) or **My team** (manager: one fold per direct report). A report with reports becomes a team fold ("Closers · Hamza"); a person with 2+ lines becomes a person fold; a person with 1 line shows the line directly.
- Folds: any number open at once; open state remembered (localStorage); a closed fold shows its unread total.
- No scroll: when more than 6 line rows are showing, rows go compact (number beside the name). If it still overflows, lines with unread or a live call stay, the rest collapse behind "+N more".
- Line row: tile (department colour), name, number, unread badge, pulsing dot while a call is live on it.
- Collapsed (64 px): icons + line tiles with unread counts.
- **Phone dock**: number/name field, "From" line picker (lines the viewer can call from), Call. During a call it becomes Mute / Hold / Transfer / End (existing call controls). Not shown on Settings.

## 3. Settings menu
Search settings box; setup card for owners ("Setup N of M done · Next: …", hidden when done); groups:
- **Phone system**: Lines, Departments, Calling (sub-pages Call flows, Queues, Hold music, Recording), Messaging (Registration, Templates, Opt-outs), AI assistants
- **Workspace**: Workspace, Team & security (Members, Roles, Security), Business verification (owner), Billing & usage (owner; Plan & credits, Usage, Invoices)
- **Your account**: My profile, Notifications & sound (new: desktop alerts, ringtone, mic/speaker test)
Open section lists its sub-pages beneath it (tabs move into the menu). Flags on items that need action. **Removed from the customer menu: Developers, Providers.** Existing routes keep working (redirects).

## 4. Lines page (replaces Phone numbers + inbox half of Departments & inboxes)
"Line" = one number + its inbox (already 1:1). List grouped by department: tile, name, number, **10DLC campaign picker inline**, one status (Ready / Can't text / No 911 address / Texting pending). Top: 10DLC meter per campaign `used/50`, amber at ≤3 spots, red "Full · attach another campaign" at 50; "+ Campaign". Filters: All, Needs attention.
Click a line → **focused line page** (replaces the list; back link), tabs: Overview (4 cards: Texting, 911, Answered by, Who can use it) · Who can use it · Calls · Texting (campaign, registration, reply/resolve targets) · Advanced (name, department, colour, port lock, release).
**Carrier and per-number cost are never shown to customers, owners included.** They stay in the ops console. Get a number (search/order/add/port) and Port requests keep every current capability.
**10DLC cap: 50 numbers per campaign (Telnyx limit), enforced server-side**: assigning a 51st number is refused with a clear error; a full campaign shows "(full)" and is not selectable.

## 5. Departments page
Cards: name, active toggle, ··· (rename, delete), People (+ Add), sub-teams inside it ("Closers team led by Hamza · 3 people"), Lines (+ Add line), default access ("Members can send & call · Change"). "Lines with no department" card. One access editor everywhere (line page, sidebar shortcut, Team member view).

## 6. Team page and reporting lines (security-critical)
- Every person has **one manager** (`reports_to`, nullable; owner at the top). Every personal line has **one owner** (nullable = unowned).
- **Access rule** (added to the existing grants, never replacing them): you can use a line if you own it, it is granted to you or your department (today's rules), or its owner is anywhere below you. Workspace owner/admin keeps full scope.
- **Managers can read, reply and call** on their reports' lines (user choice, option 1). Managers are NOT rung for reports' calls; missed calls reach them through the bell.
- Sub-team = a team lead with reports, optional team name (e.g. "Closers"), shown inside the department.
- Team page: indented tree with fixed columns (Person, Department · sub-team, Their lines, Sees N), fold per manager, presence dot, "You" badge, search, ··· menu (change manager, title, team name, remove). Drag a person onto someone to move them (their team moves too); drag a line chip to change owner; Undo toast; loops refused.
- **"Needs a spot" strip**: people not placed, lines with no owner, people with no line ("No line yet · Assign one").
- Person leaves → their lines go to their manager. Moving a manager moves everyone below.

## Out of scope
Mobile drag-and-drop (··· menu covers it), analytics page contents, new notification kinds (voicemail / waiting-on-reply counters come from queries, not bell rows).
