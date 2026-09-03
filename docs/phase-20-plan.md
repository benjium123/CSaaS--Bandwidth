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
