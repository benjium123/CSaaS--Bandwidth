# CSaaS — Phases 15–19: Quo-style UI, Tiered Inboxes, Multi-Provider Live

Status: DRAFT — awaiting user approval (2026-09-01)
Author: Fable 5 (Tier 1). Implementers: DeepSeek V4 Pro / Sonnet 5. Reviewer: Opus 5.

## Why now
- Outbound calling error diagnosed 2026-09-01: Bandwidth account 9903389 is in FREE TRIAL —
  `402 Payment Required: "Outbound call must be to your verified mobile number during your
  free trial."` Not a code bug. See Go-Live Checklist below.
- User wants: (a) interface closer to Quo (three-pane dark inbox, inboxes in sidebar,
  calls+SMS unified per contact), (b) tiered inbox access (admin → departments → employees,
  numbers at each level, shareable), (c) top-5 providers wired with in-app credentials,
  number purchasing, and cost tracking.

## Go-Live Checklist (user/operator actions, no code — do in parallel with phases)
1. **Bandwidth trial → paid** (user): add payment / complete verification on account 9903389.
   Until then outbound calls only reach the verified mobile number.
2. **B2** (user): Bandwidth portal — create inbound SIP peer → `144.126.152.175:5060`,
   assign +19404060664 to it.
3. **B1** (user): paste `TELNYX_API_KEY` + `TELNYX_MESSAGING_PROFILE_ID` into `/opt/csaas/.env`.
4. **B4** (user): paste `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` into `.env`.
5. **I1** (operator, needs explicit user authorization — shared nginx): add `/status` +
   `/livekit/` locations per RUNBOOK, `nginx -t && reload`, set `LIVEKIT_PUBLIC_URL`.

---

## Phase 15 — Tiered inboxes: departments + inbox access model (backend-first vertical slice)

**Goal.** Introduce Departments and first-class Inboxes so number access is tiered:
admin sees everything; a department owns numbers its members inherit; individual
employees can be granted specific inboxes; an inbox (or a whole department) can be
shared onward. Enforced for BOTH SMS threads and calls (visibility, sending, dialing,
inbound ring fan-out).

**Data model (new migration 0016).**
- `departments` (org_id, name, slug, is_active)
- `department_members` (department_id, user_id) unique pair
- `inboxes` (org_id, name, number_id FK→org_numbers UNIQUE, color/icon) — one inbox per number,
  auto-created when a number is added
- `inbox_grants` (inbox_id, grantee_type: `department`|`user`, grantee_id, role: `member`|`viewer`)
  unique (inbox_id, grantee_type, grantee_id)
- Access resolution: org `owner`/`admin` role ⇒ all inboxes; else union of user grants +
  grants to departments the user belongs to. `viewer` = read-only (no send/dial from that number).

**Enforcement points.**
- Thread list/read: filter `message_threads.our_e164` to accessible numbers.
- Message send + call place: sending/dialing `from` a number requires `member` access.
- Calls list/read: same filter on `our_e164`.
- Inbound ring + unread fan-out: notify users with access to that number's inbox.
- New scopes: `departments:read`, `departments:manage`; reuse `inbox:manage` for grant admin.

**API.** CRUD: `/api/v1/departments`, `/api/v1/inboxes`, `/api/v1/inboxes/{id}/grants`;
`GET /api/v1/inboxes/mine` (drives sidebar).

**Test spec (headline).** Access-resolution unit matrix (admin / dept member / direct grant /
viewer / no grant); thread+call visibility filters; send/dial 403 for non-members; ring
fan-out respects grants; migration up/down; tenant isolation (org_id hooks) on all new tables.

**Deploy:** yes. **Est. files:** ~14 backend + migration. Minimal Team-page UI to manage
departments/grants ships here too (full sidebar comes in P16).

---

## Phase 16 — Quo-style interface (frontend revamp)

**Goal.** Rebuild the console shell to mirror the Quo screenshot: dark three-pane layout.
- **Sidebar:** workspace header, Search, Activity, Contacts, Analytics, Settings; an
  **Inboxes** section listing each accessible inbox (name + number, from `/inboxes/mine`);
  admin sees all grouped by department.
- **Middle pane:** tabs **Chats / Calls / Tasks**, filter chips (Open ▾, Unread,
  Unresponded, Filter), conversation rows with avatar/initials, name or number, snippet
  (incl. "Called you", "Missed call"), date.
- **Right pane (main):** unified per-contact timeline — SMS bubbles AND call events
  (missed call, outbound call, voicemail w/ transcript) interleaved chronologically;
  header with contact + call/message action buttons; composer at bottom.
- **Far-right contact panel:** name/avatar, call+message buttons, Tasks, Contact fields
  (company, role, phone, email, address), shared-access indicator, Notes.
- Selecting an inbox scopes the middle pane to that number; an "All" view for admins.

**Backend support.** New `GET /api/v1/conversations/{contact}/timeline` merging messages +
calls for (our_e164, contact_e164); snippet fields on thread list; call rows surface
failure reason (so a Bandwidth 402 shows as a red "Call failed: …" event, not silence).

**Test spec (headline).** Timeline merge ordering; snippet correctness for call events;
inbox scoping of list; frontend vitest for sidebar rendering from grants; e2e smoke
(login → pick inbox → open thread → see call event → send message).

**Deploy:** yes. Frontend-heavy (~15–20 files) + 2–3 backend files.

---

## Phase 17 — Provider accounts in-app (DB credentials, per-org, all 5 providers)

**Goal.** Stop requiring env edits: an org admin pastes API credentials in a Providers
page and the backend goes live with that provider. Providers: **Bandwidth, Telnyx,
Twilio, Plivo, SignalWire** (adapters already exist for all five).

**Data model (migration 0017).**
- `provider_accounts` (org_id, provider, label, credentials_encrypted, status:
  `unverified`|`active`|`failed`, last_probe_at, last_probe_detail) — credentials encrypted
  at rest (Fernet, `CREDENTIALS_MASTER_KEY` in env; key stays in env, secrets in DB).

**Behavior.**
- `build_registry()` becomes per-org: DB accounts first, env-var fallback (keeps current
  deployment working unchanged).
- `POST /provider-accounts/{id}/probe` reuses existing carrier probe to verify creds before
  activation; credential fields are provider-specific schemas (documented per adapter).
- Providers page UI: add/edit account (secrets write-only, never echoed), probe button,
  status, which numbers ride on it.

**Test spec (headline).** Encrypt/decrypt round-trip; registry resolution order (DB → env);
probe success/failure state machine; secrets never in API responses/logs; RBAC
(`settings:write`); tenant isolation.

**Deploy:** yes.

---

## Phase 18 — Number search, purchase & inventory (per provider)

**Goal.** From the Numbers page: search available numbers (area code / city / type) at any
active provider account, buy one, and it lands in `org_numbers` with an auto-created inbox
(P15) — assignable to a department or employee immediately. Telnyx purchasing already
exists (P4); extend the same `as_provider()` interface to Bandwidth, Twilio, Plivo,
SignalWire (Bandwidth ordering is async — poll order status).

**Data model.** Extend `org_numbers`: `provider_account_id` FK, `purchase_cost_cents`,
`monthly_cost_cents`, `purchased_at`. Number release endpoint (soft: mark released,
provider release call, keep history).

**Test spec (headline).** Search/purchase mocked per provider; async Bandwidth order
polling; purchased number → inbox auto-create → grantable; release flow; failure surfaces
provider error verbatim.

**Deploy:** yes.

---

## Phase 19 — Provider cost tracking & spend dashboard

**Goal.** Know what each backend provider costs.
- `provider_rates` (org_id nullable for defaults, provider, metric, unit_cost_micros) —
  editable rate card seeded with current list prices per provider.
- Daily spend rollup: existing `usage_records` × rates, split **per provider** (add
  `carrier` dimension to rollup) + number MRC from P18 columns.
- Providers page: spend this month per provider, per number, per metric; Analytics tile.

**Test spec (headline).** Rollup math (segments/minutes × rate + MRC proration); carrier
dimension correctness; rate-card edit RBAC; zero-rate default safety.

**Deploy:** yes.

---

## Sequencing & rules
- Order: P15 → P16 → P17 → P18 → P19 (each vertical, testable, deployable).
- Per delegation rules: Fable writes each phase's plan+test spec+guardrails; DeepSeek V4
  Pro / Sonnet 5 implement; Opus 5 reviews; Fable final-reviews. Schema/migrations
  authored by Fable only.
- Everything additive on the VPS, `/opt/csaas` only, deploy via `./deploy/deploy.sh`.
- Existing 911 backend + 66 frontend tests must stay green each phase.
