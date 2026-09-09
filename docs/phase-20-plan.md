# Phase 20 — Simplicity: a two-surface product (Work vs Settings)

## Product principle (Fable, 2026-09-03)
We sell simplicity. A user sees ONE working surface and nothing else; an admin gets ONE
grouped Settings area. No provider names on working pages. No engineering vocabulary
(carrier, breaker, registry, env, DLR, E.164) anywhere a customer can see. Every admin
form follows the same pattern: pick one thing from a dropdown → see only that thing's
options → Save → a status pill. Empty states teach the next step. Nothing dead.

## Surface 1 — Work (everyone)
Sidebar (left rail): **Inbox** (home), **Contacts**, **Calls** (history; optional — calls
already live in the inbox timeline), **Campaigns** (only if the user holds
campaigns:read), and a **Settings** gear (only if the user holds any settings/admin
permission). That is the whole nav. The softphone dock stays global.
- Inbox: exactly the P16 three-pane. Remove from it: campaign assignment, number
  ordering, grants UI, carrier labels. Inbox names only (no "(env)" / provider tags).
- Contacts: list + detail; no custom-field definitions here (that's Settings).
- Legacy pages (Dashboard, Lists, Flows, Queues, Numbers, Providers, Platform, Team,
  Security, Agent, Appointments) are no longer top-level; each becomes a Settings
  section or is folded into Inbox/Contacts.

## Surface 2 — Settings (admins; sectioned left nav inside /settings)
1. **Workspace** — name, timezone, business hours, logo.
2. **Team** — members, roles, invites (existing Team page), 2FA/security policy.
3. **Departments & Inboxes** — departments, per-inbox name/colour, who can see/use
   each inbox (P15 grants UI; plain words: "Can view", "Can send & call").
4. **Phone numbers** — table of numbers (name, number, provider label, status, cost,
   inbox). Buttons: **Get a number** (dropdown: provider → area code/type → results →
   Order), **Add existing number**, **Release**. 10DLC status per number with a plain
   explanation and a link to the Registration section.
5. **Providers** — ONE dropdown "Connect a provider" (Bandwidth / Telnyx / Twilio /
   Plivo / SignalWire) → shows only that provider's fields with a one-line hint per
   field ("Find this under Auth → API Keys in the Telnyx portal") → Save → Probe →
   status pill. Below: "Connected providers" list (label, provider, status, numbers,
   spend this month, Rates, Disable). The carrier-health/routing-policy panel moves
   here under an "Advanced" disclosure, collapsed by default.
6. **Messaging** — compliance (quiet hours, opt-out keywords, DNC), templates,
   auto-replies, **Registration** (10DLC brand/campaign, toll-free verification) with
   a step-by-step status tracker ("Brand approved → Campaign pending (1–2 days)").
7. **Calling** — call flows/IVR, queues, voicemail, recording, business hours (existing
   Flows/Queues pages, restyled), softphone preferences.
8. **AI** — agent profiles, knowledge base, appointments (existing Agent/Appointments).
9. **Billing & usage** — spend (P19 tile + per provider), rates drawer, usage.
10. **Developers** — API keys, outbound webhooks (existing Platform page).

## Design system rules (implementer must follow)
- One theme (dark, the P16 palette) applied globally via the existing `.dark` tokens on
  the app shell; light legacy pages are restyled to tokens, no page keeps its own palette.
- Shared primitives only (Button, Input, Select, Pill, Card, Section, EmptyState,
  MutationStatus, Drawer). No raw `<button>`/`<select>` in pages.
- Every list has: loading skeleton, error state with retry, empty state with a CTA.
- Every mutation: pending state, inline error, success confirmation.
- Words: "provider" not "carrier"; "number" not "DID"; "can send & call" not "member".
- Role gating: nav and buttons render only for permissions the user holds (from the
  membership role); backend remains the authority.
- Mobile: Work surface usable at 390px (list ↔ detail toggle); Settings responsive.

## First-run onboarding (admin, appears until done)
Checklist card on Inbox when the org has no active provider or no number or no team:
1 Connect a provider → 2 Get a number → 3 Invite your team → 4 Register for SMS (10DLC).
Each step deep-links into the Settings section.

## Backend support (small)
- `GET /api/v1/me/capabilities` → the permission set + org summary (has_provider,
  has_number, member_count, registration_state) for nav gating and onboarding.
- Numbers list gains `inbox_name`; providers list gains `numbers_count`, `spend_mtd`.
- Nothing else; all existing endpoints stay.

## Guardrails / execution
- Frontend: implementers may restructure src/pages, src/components, src/App.tsx freely
  (this is a re-IA), but must keep every existing test passing or migrate it with the
  page; no new deps without approval. Backend: only the small endpoints above.
- Sequence: after the bug-fix round → DeepSeek drafts per section from this spec →
  Sonnet integrates → Opus reviews (UX correctness + contract) → Fable sign-off → deploy.
- Definition of done: an agent-role user sees only Inbox/Contacts (+Calls); an admin
  configures a new provider and buys a number in under 3 minutes from a blank org;
  no provider name appears on the Inbox; frontend + backend suites green.

## Later (not P20)
Self-serve signup + Stripe billing (today registration is invite-only); white-label.

## Positioning → product requirements (user, 2026-09-03: "simple, ready to use,
## plug and play with best routing")
- **Ready to use** = a new org gets working defaults with zero configuration: quiet
  hours (8am–9pm local), opt-out keywords + auto-replies, HELP/STOP templates, a default
  call flow (ring the inbox's members → voicemail), recording off, AI off. Seeded on org
  creation (backend: `services/defaults.py`, idempotent) — part of P20.
- **Plug and play** = P20's provider dropdown + "Get a number" + the first-run checklist.
  Honest constraint to state in marketing: SMS registration (10DLC or toll-free
  verification) is carrier-mandated and takes days; the product tracks it, it cannot
  skip it. Voice and inbound SMS work immediately.
- **Best routing** = Phase 21 "Smart routing" (after P20): per message/call the system
  picks the route automatically from (1) capability, (2) provider health/breaker state,
  (3) number reputation, (4) cost from the P19 rate card, (5) org preference — and
  records WHY on the message/call ("Sent via Telnyx — cheapest healthy route"). One
  customer-facing switch: Smart routing On (default) / Prefer a provider. Failover
  across providers becomes the default for outbound SMS when Smart routing is on
  (today it is opt-in). Voice failover (D28) lands in the same phase. Existing routing
  fabric (P3b/P14: RoutePlan, breakers, pinned_carrier) is the base; this adds the
  ranking function + explainability + defaults.

## Addendum (Fable, 2026-09-10) — OpenPhone-style layout, decided by the user
User direction: "make the interface more like OpenPhone; combine settings in one place; in
the inbox we can start a call or a new text". Binding for the P20 implementer.

### Layout (desktop)
1. **Left rail** (56 px, icons + tooltip; labels appear on hover/expand): Inbox, Contacts,
   Calls, Campaigns (only with campaigns:read), then at the bottom the org avatar/switcher
   and the **Settings** gear (only with any settings/admin permission). Nothing else, ever.
2. **Inbox column** (220 px, inside Inbox): "All conversations", then **one row per inbox**
   the user can see (P15 grants) with the inbox colour dot, name, and unread count — exactly
   OpenPhone's phone-number list. Below it: Important, Unresponded, Snoozed (P26). A **"+ New"**
   button at the top of this column opens the existing NewConversationPanel (New text /
   New call) — this is the primary way to start anything; keep the header "+ New" too.
3. **Conversation list** (320 px): the P16 list with search, filter dropdown, star, unread dot.
4. **Thread** (flex): unified SMS/call timeline; composer at the bottom with Reply | Note
   (P26) and a **call button** in the thread header (dials the contact from this inbox's
   number via the softphone dock — no page change).
5. **Contact panel** (300 px, collapsible, storageKey): name, numbers, owner/team (P22),
   tags, notes, recent activity; "Open contact" link (404s when the visibility policy
   excludes the user — documented P22 rule).
6. **Global search** (⌘K) over contacts, numbers, and conversations.
7. **Softphone dock**: bottom-right, always mounted; the dial pad is a slide-over opened
   from the rail's Calls icon (long-press/secondary) or from any phone number shown in
   the UI (every phone number is clickable → Text / Call menu).
Routes: `/inbox/:inboxId?/:threadId?`, `/contacts/:contactId?`, `/calls`, `/campaigns`,
`/settings/:section`. Legacy routes redirect: /dashboard → /inbox (until P30 gives it
Reports), /lists → /contacts?tab=lists (P27 folds Lists), /agent and /appointments →
/settings/ai, /flows and /queues → /settings/calling, /numbers → /settings/numbers,
/providers → /settings/providers, /security and /team → /settings/team, /platform →
/settings/developers, /settings/inboxes → /settings/inboxes (kept), /inbox/legacy removed.

### Settings: ONE place
`/settings` renders a two-column page: sectioned left nav (Workspace, Team, Departments &
Inboxes, Phone numbers, Providers, Messaging, Calling, AI, Billing & usage, Developers —
the ten sections above), content on the right. Existing pages are moved in as sections
and restyled to the shared primitives; no page keeps its own palette. P22's Roles tab and
Contact visibility radio live under Team. Security (2FA) is a sub-tab of Team.

### Mobile (≤ 640 px)
Rail becomes a bottom tab bar (Inbox, Contacts, Calls, Settings); the inbox column and
contact panel become sheets; list ↔ thread toggle; composer sticks to the bottom.

### Shared primitives to build first (frontend/src/components/ui/)
Button, Input, Select, Pill, Card, Section, EmptyState, MutationStatus, Drawer, Sheet,
Tabs, Collapsible (with storageKey), Kbd. Build these in P20a; migrate pages in P20b.

### Done means
An agent sees rail + inbox column + list + thread + contact panel and can text or call from
"+ New", from the thread header, and from any phone number; an admin finds every setting
under /settings in under 10 seconds; no legacy route renders a page; suites green.
