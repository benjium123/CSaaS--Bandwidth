# Phase 2 — Contacts, conversations, and the unified inbox

> Refined by Fable (Tier-1) 2026-08-26. Workstreams: WS-2 (Messaging), WS-7 (Console),
> WS-0 (surgical: RBAC keys, 2FA columns, settings fields).
> Builds on P0 + P1a (99 tests green, session-layer tenancy, carrier layer, webhook
> ingestion, thread buckets, compliance seam).
> **R1 is still open — no Bandwidth account.** Like P1, this phase splits into **P2a**
> (everything buildable, testable and *demonstrable in a browser* today, via a dev-only
> loopback carrier that drives the real ingestion pipeline) and **P2b** (public deploy +
> the live-handset conversation, blocked on R1/P1b). P2a is the entire codebase of the
> phase; P2b adds zero production code.

## Goal

Ship the first vertical slice a human can *use*: a React console (login → org →
unified inbox → thread view → compose → live updates) backed by contacts, companies,
tags, notes, custom fields, conversation state (open/closed, assignment, read/unread,
labels), and a sticky-sender selection that never silently jumps a conversation to a
different number. Plus the 2FA/TOTP that P0 DR-6 deliberately parked here. Every new
table rides `TenantScoped`; every list the inbox renders is produced by a fixed number
of queries with a test that fails if N+1 regresses.

**P2a gate (today, no carrier):** full backend suite green on Python 3.10 + SQLite and
on the Postgres CI job; frontend typecheck + vitest suite + production build green on
Node 22; OpenAPI-drift check green; and a *demo*: with `LOOPBACK_CARRIER_ENABLED=true`,
hold a two-way conversation entirely in the browser (send → simulated DLRs tick to
`delivered` → simulated inbound reply appears via polling), with the thread attributed
to a contact whose name renders in the inbox.

**P2b gate (blocked on R1/P1b):** the console deployed on the VPS behind nginx TLS;
log in from a real browser over the internet, hold a full two-way SMS conversation with
a real handset, thread correctly attributed to a contact. This is PHASES.md's P2 gate.

---

## Decision Record (settled for P2 — do not relitigate in implementation)

### DR-1: Frontend stack, serving, auth, and — critically — testing

**Stack (per D3):** React 18 + Vite + TypeScript (strict) + Tailwind + shadcn/ui
(vendored components — shadcn copies source into `src/components/ui/`, so the runtime
deps are only radix primitives + cva/clsx/tailwind-merge + lucide-react). Router:
`react-router-dom`. Server state: `@tanstack/react-query` — its polling/caching
semantics replace a hand-rolled cache we would otherwise get wrong. No other runtime
deps without Fable approval. Node v22.19.0 is present; no Docker needed anywhere in the
frontend loop.

**Dev serving:** `npm run dev` (Vite, port 5173) with a Vite proxy `/api` + `/healthz`
→ `http://127.0.0.1:8080`. Same-origin in dev, so CORS never bites; the backend's
default `cors_origins=http://localhost:5173` stays as a belt-and-braces.

**Prod serving:** `vite build` → static files served by **nginx** (which P1b already
requires for the webhook), `try_files ... /index.html` SPA fallback, `/api/` +
`/healthz` proxied to 127.0.0.1:8080. The API container stays pure Python; deploy.sh
builds the frontend **locally** (Node is a dev-machine tool, not a VPS install) and
ships `frontend/dist` inside the archive.

**Auth:** JWT Bearer + `X-Org-Id`, exactly the P0 contract. The API client is one
module (`src/api/client.ts`) that attaches both headers; token + selected org persist
in `localStorage`; any 401 clears state and routes to login. XSS-vs-cookie tradeoff
acknowledged: we have no cookie/CSRF infrastructure and the API is header-driven;
revisit (httpOnly cookie + CSRF) if/when P13 makes the API public-facing for third
parties. No token in URLs, ever.

**Testing — the honest answer:** there is no browser automation on this machine, so
the P2 frontend test layer is **Vitest + @testing-library/react + jsdom + user-event**,
which runs on Node 22 today with zero extra infrastructure. The API client is an
injectable interface; component tests stub it (no MSW — one less dependency, fully
deterministic). Three mechanical guards make this real rather than decorative:
1. **`tsc --noEmit` (strict) is a CI job** — the type layer is generated from the
   backend (next point), so a large class of contract bugs is compile-time.
2. **OpenAPI drift check:** `backend/scripts/export_openapi.py` writes
   `frontend/openapi.json` from the FastAPI app (no server needed);
   `openapi-typescript` generates `src/api/types.gen.ts`. Both files are committed; CI
   regenerates and `git diff --exit-code`s them. The frontend cannot silently disagree
   with the backend about a response shape.
3. **Backend contract tests compensate for the missing E2E layer:** every endpoint the
   console consumes has an integration test asserting the exact JSON shape and status
   the client code relies on (the inbox aggregate test enumerates its keys).

**Deferred, by name:** real-browser E2E (Playwright) lands in **P6** with the softphone
spike — the softphone cannot be tested without a browser anyway, so that is where the
Playwright infrastructure earns its install. Until then the compensating controls above
are the contract.

### DR-2: Loopback carrier — the demo and dev loop must not wait for R1

A frontend phase with `carrier=None` (today's reality) could only ever demo a 503.
So P2 adds `app/providers/loopback.py`: a dev-only `MessagingCarrier`
(`name="loopback"`) that accepts sends (`SendResult("accepted", "loopback-<uuid>", None)`)
and then drives **the real ingestion service** (`ingest_event` with constructed
`DeliveryReceipt`/`InboundMessage` events, carrier name `"loopback"`) through its own
DB sessions: `message-sending` + `message-delivered` after a short delay, and an echo
inbound reply (`"echo: <text>"` from the contact's number) after ~2 s. Everything
downstream — state machine, idempotency ledger, thread upsert, contact linkage, unread
derivation, polling — is exercised for real; only the PSTN is simulated.

Guardrails: enabled **only** by `LOOPBACK_CARRIER_ENABLED=true`; boot **fails** (config
validator) if it is true in production, or true while `BANDWIDTH_ENABLED=true`
(ambiguous carrier). The simulator is `auto` in dev (asyncio tasks) and driven
synchronously in tests via `await carrier.drain()` — no sleeps in the suite.

### DR-3: Contacts attach to threads additively — no destructive migration

Model (Chatwoot's conversation/contact/inbox/label shape, translated to our stack):

- **contacts** (TenantScoped): `display_name`, `first_name`, `last_name`,
  `company_id` FK nullable, `attributes` PortableJSON (custom-field values).
- **contact_phones** (TenantScoped): `contact_id` FK CASCADE, `e164`, `label`,
  `is_primary`; **unique `(org_id, e164)`** — one phone belongs to at most one contact
  per org. This constraint is what makes phone→contact resolution deterministic and is
  the structural guard against the most common dupe vector.
- **companies**, **tags** (+ `contact_tags`), **contact_notes**,
  **custom_field_defs** (org-level: `key`, `label`, `kind` ∈ text|number|date|select,
  `options` JSON) — values live in `contacts.attributes`, validated against the defs on
  write. **JSON, not EAV**: v1 has no per-custom-field indexed querying requirement;
  P11's dynamic segments will query JSONB on Postgres if needed; EAV would cost joins,
  migration weight and SQLite parity for nothing we ship this year.
- **message_threads** gains nullable `contact_id` FK (**ON DELETE SET NULL** — threads
  and messages are immutable comms records and survive contact deletion), plus the
  DR-5 conversation-state columns. All additive; migration `0003` touches no existing
  row destructively.

**Linkage rules** (one function, `resolve_or_create_contact(session, org_id, e164)`,
used by both directions):
- Inbound from an unknown number → **auto-create** a minimal contact
  (`display_name` = the E.164, one phone row) and stamp the thread. Race-safe the same
  way `upsert_thread` is: insert, catch `IntegrityError` on `(org_id, e164)`,
  re-select.
- Outbound send that creates a thread → same resolve-or-create.
- **Contact created/edited after messages exist:** adding phone `X` to a contact calls
  `link_threads_for_phone(org, X, contact_id)` — stamps every thread with
  `contact_e164 == X` whose `contact_id` is NULL *or* pointed at an auto-created
  placeholder now superseded (in P2: NULL only; reassignment of a phone between
  contacts re-links in the same call). Removing a phone leaves threads pointing at the
  contact (history is history).
- **Pre-P2 threads:** linked lazily (next message through the thread stamps it) *and*
  eagerly by `scripts/backfill_contact_links.py` (idempotent; run once per deploy).
- **Dedupe/merge:** merge is P11 per SPEC. P2's contribution is prevention: the unique
  phone constraint means two contacts can never claim the same number, so P11's merge
  is a re-parenting exercise, not a data-repair one. No merge endpoint in P2.

### DR-4: Sticky sender — never silently jump a conversation's number

`select_sender(session, org_id, contact_e164, requested, allow_reassign)` replaces
P1's `resolve_from_number` (which 422'd on multiple active numbers):

1. `requested` given → must be an active number of this org (422 otherwise, including
   numbers owned by other orgs — unchanged P1 teeth). An explicit `from` that differs
   from an existing thread's number deliberately opens a separate `(our, contact)`
   bucket — explicit overrides are the operator's business.
2. No `requested` → **sticky**: the most recent thread (by `last_message_at`) for this
   `contact_e164` wins → reuse its `our_e164` **iff that number is still active**.
3. Sticky number inactive (**gotcha #18** — the silent-jump bug): the send **fails
   422 `sticky_sender_unavailable`** unless the caller passed `allow_reassign=true`;
   with it, fall through to (4) and log WARNING `sticky_sender_reassigned` with both
   numbers. A conversation changes sender number only on an explicit, logged decision —
   never as a silent side effect of pool state.
4. No prior thread → deterministic assignment:
   `sha256(contact_e164) % len(active_numbers sorted by e164)` — a pure function, so
   the same contact always lands on the same number even across restarts, spreading
   contacts across the pool without a counter table. (Throughput-aware pacing across
   the pool is P11; this is conversation affinity, not rate control.)

**Gotcha #1 (STOP suppresses the whole pool) — data-model obligation only:** P3 owns
the logic; P2 must not make it impossible. Enforced by two invariants stated here and
checked in review: (a) **nothing in P2 stores opt-out or consent state keyed by our
number** — P3's ledger keys on `(org_id, contact_e164)`; (b) the compliance seam
already receives `(org_id, draft.to_e164)` before any send, which is exactly the
pool-wide key. There is no `number_pools` table in P2 — the pool is "the org's active
numbers"; when P11 introduces explicit pools they partition *sending*, never opt-out.

### DR-5: Conversation state — ruthless cut

**IN (P2):**
- `status` ∈ `open | closed` (String(8), server_default `'open'`, existing rows
  backfilled `'open'`). **A new inbound message reopens a closed thread** — set in the
  same transaction as the deduped insert, so replay-safe by construction.
- `assigned_user_id` (GUID FK→users SET NULL, nullable). Assignee must be a member of
  the org (validated against `org_memberships`).
- **Read state: derived, not counted.** `last_read_at` (tz DateTime, nullable) on the
  thread; unread = COUNT of inbound messages with `created_at > COALESCE(last_read_at,
  epoch)`. No denormalized counter — a counter incremented from a webhook handler is
  exactly the "increment side effect" D6 bans; a derived count cannot drift, and the
  aggregate query (DR-7) pays for it once per page. Org-level read state, one cursor
  per thread.
- **Labels:** `thread_labels` join to the same `tags` table (`tags` carries no kind
  column; a tag is a tag). Recorded SPEC deviation: "message tagging (P2)" ships as
  **thread-level labels**; per-message tags are deferred to P13 — nobody tags
  individual SMS bubbles in v1.

**OUT (deferred, by name):** `snoozed` (needs a timer to be anything but `closed` —
P13, with notifications); **per-user read cursors** (P13 — org-level is honest for the
seat counts P2 serves, and the schema change is additive when it comes); internal/private
thread notes (P13; contact notes ARE in P2); SLA timers, CSAT, canned responses
(P13/P10); priority field (never requested).

### DR-6: Real-time = polling. WebSockets/SSE deferred with reasons

The inbox needs inbound messages to appear without a refresh. **Polling wins P2:**
- TanStack Query `refetchInterval: 4000` on the inbox aggregate and 2500 ms on the
  open thread's messages (keyset `after` param → each poll transfers only news),
  paused when `document.visibilityState === 'hidden'`.
- Correct by construction: no connection lifecycle, no auth-over-WS story (EventSource
  cannot send `Authorization` headers at all; a WS needs its own ticket flow), no
  reconnect/backoff/missed-event replay code to get wrong, nothing new to deploy.
- Cost honest: a 4 s poll of one ≤6-query endpoint per active seat is nothing at P2
  scale, and every poll is also the failure-recovery path — there is no "missed event"
  concept to test.
- **Does not paint us into a corner:** the frontend isolates freshness behind query
  hooks — swapping the transport later touches `src/api/hooks.ts` only. P7's WebSocket
  media server is **carrier media frames**, latency-critical and pinned near the
  carrier PoP; multiplexing browser inbox fan-out onto it would couple UI concerns to
  the one subsystem with a hard real-time budget. A browser event channel is a P13
  (notifications) deliverable with its own design.

### DR-7: Inbox aggregate — fixed query count, keyset pagination, N+1 test

`GET /api/v1/inbox/threads` returns everything a row of the inbox renders. Query
shape — **query count is constant in page size**:
1. Page of threads: filters (`status`, `assigned` = `me|unassigned|<user_id>`, `q`
   matching contact `display_name` ILIKE or `contact_e164` LIKE, `label`), ordered
   `last_message_at DESC, id DESC`, **keyset cursor** (opaque base64 of
   `"<iso_ts>|<uuid>"`; `limit` default 30, max 100). Offset pagination is out — pages
   shift under live inserts. Migration `0003` backfills NULL `last_message_at` to
   `created_at` and the code always maintains it, so the sort key is total.
2. Last-message previews: one window-function query
   (`ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY created_at DESC, id DESC)`)
   over `thread_id IN :page_ids` — SQLite ≥3.25 and PG both support it; the bundled
   SQLite on Python 3.10.10 is 3.37+.
3. Unread counts: one grouped count join (`inbound AND created_at > COALESCE(last_read_at, epoch)`)
   over the page ids.
4. Contacts, 5. assignees (users), 6. labels: three `IN (:page_ids)` lookups.

**The regression test that FAILS on N+1:** a conftest `query_counter` fixture hooks
`before_cursor_execute` on `engine.sync_engine`. Seed 30 threads × 5 messages;
assert (a) `count(limit=25) == count(limit=5)` — query count must be independent of
page size — and (b) an absolute ceiling of **≤ 8 statements** per request. Any
lazy-load slipping into the serializer breaks (a) immediately.

### DR-8: 2FA/TOTP — honoring P0 DR-6, minimal shape

P0 moved 2FA here because the console is the enrollment surface, and P2b is the moment
the login endpoint faces the public internet — this does not slip again. Scope:
- `pyotp` (new dep). `users` gains `totp_secret` (nullable, **Fernet-encrypted with
  `CREDENTIAL_ENCRYPTION_KEY`** via P0's `encrypt_credential` — the first real consumer
  of that key), `totp_enabled` bool default false, `totp_last_used_step` int nullable
  (blocks replay of a just-used code).
- Enrollment requires the Fernet key: without it, enroll answers 503
  `feature_unavailable` (no plaintext-secret fallback branch — branching secret
  storage is bug bait). Dev `.env` carries a key.
- Flow: `POST /auth/2fa/enroll` (auth'd → provisioning URI + secret; UI renders a QR
  client-side) → `POST /auth/2fa/activate {code}` (window ±1 step) → thereafter
  `POST /auth/login` returns `{requires_2fa: true, pending_token}` (5-min JWT, scope
  claim `"2fa-pending"`, **not accepted by `get_current_user`**) →
  `POST /auth/2fa/verify {pending_token, code}` → real token.
  `POST /auth/2fa/disable {code}` clears it.
- **NOT in P2:** backup codes, remember-device, SMS fallback, admin reset UI
  (recovery in v1 = operator clears the columns; documented in PROGRESS).

### DR-9: RBAC additions + system-role data migration

New catalogue key **`inbox:manage`** (assign / open / close / label threads), granted
to owner (`*`), admin (recompute), **and agent** — agents work the inbox; the tested
RBAC deny path remains agent×`members:read` (untouched, so the P0 test stands, and the
console degrades: agents get "Assign to me / Unassign" instead of a member picker,
driven by the 403 on the members endpoint; the aggregate still shows assignee *names*
because the server joins them — the permission gates the member-list endpoint, not the
join). Contacts/companies/tags/notes ride existing `contacts:read/write`; custom-field
defs ride `settings:read/write`. Because role permissions are JSON rows seeded at org
creation, migration `0003` **re-seeds `is_system` roles by name from literals inlined
in the migration** (idempotent; never touches non-system roles).

### DR-10: Surgical-edit budget on P0/P1 files

P2 must touch some frozen files. The full list and the permitted scope of each edit is
in Allowed Files; anything beyond that scope = guardrail violation. Notable:
`resolve_from_number` is **replaced** by `select_sender` (DR-4) — if any P1 test
asserted the old multi-number-ambiguous 422, update that assertion to the sticky
contract and note it in PROGRESS (recorded deviation, not a silent edit).

---

## Pre-dependencies

- P0 + P1a merged; suite green (99 passed baseline).
- Backend deps added to `backend/pyproject.toml`: **`pyotp>=2.9`**, **`cryptography`**
  (Fernet — may already be transitively present; pin it as a direct dep now that code
  imports it). Nothing else without Fable approval.
- Frontend created under `frontend/` with `npm create vite@latest` (react-ts) then
  pinned deps: `react`, `react-dom`, `react-router-dom`, `@tanstack/react-query`,
  `tailwindcss` (+postcss/autoprefixer per Tailwind install), shadcn/ui vendored
  components and their peer deps (`class-variance-authority`, `clsx`,
  `tailwind-merge`, `lucide-react`, radix packages as pulled by the specific
  components used). Dev: `typescript`, `vitest`, `jsdom`,
  `@testing-library/react`, `@testing-library/user-event`, `@testing-library/jest-dom`,
  `openapi-typescript`, `@vitejs/plugin-react`. `package-lock.json` is committed.
- Dev `.env` additions (operator, not implementer): `LOOPBACK_CARRIER_ENABLED=true`,
  a dev `CREDENTIAL_ENCRYPTION_KEY` (Fernet). `.env.example` gains the loopback flag
  line — **Fable applies this one-line edit**, the implementer does not touch it.
- **P2b only (user-owned):** R1 resolved; P1b's nginx TLS + hostname done; operator
  session for the deploy.

## Allowed Files (implementer may create/read/write — nothing else)

New backend files:
```
backend/app/models/contacts.py
backend/app/services/contacts.py
backend/app/services/inbox.py
backend/app/services/sender.py
backend/app/providers/loopback.py
backend/app/api/routes/contacts.py          (contacts + companies + tags + notes + custom-field defs)
backend/app/api/routes/inbox.py             (aggregate, thread PATCH, read, labels)
backend/app/api/routes/twofa.py
backend/migrations/versions/0003_inbox_contacts.py
backend/scripts/export_openapi.py
backend/scripts/backfill_contact_links.py
backend/tests/test_contacts.py
backend/tests/test_inbox_aggregate.py       (includes the N+1 query-count test)
backend/tests/test_thread_state.py
backend/tests/test_sticky_sender.py
backend/tests/test_loopback_carrier.py
backend/tests/test_twofa.py
```
New frontend tree (globbed — everything under it is in scope):
```
frontend/**            (package.json, lockfile, vite/ts/tailwind configs, index.html,
                        openapi.json, src/**, src/api/types.gen.ts, tests colocated
                        as src/**/*.test.ts(x))
```
Surgical edits ONLY, scope stated — nothing else in these files moves:
```
backend/pyproject.toml                      (add pyotp, cryptography; nothing removed)
backend/app/config.py                       (add loopback_carrier_enabled field + its two
                                             validator rules from DR-2; nothing else)
backend/app/models/rbac.py                  (add "inbox:manage" to PERMISSIONS + agent/admin
                                             SYSTEM_ROLES lists; nothing else)
backend/app/models/user.py                  (add totp_secret / totp_enabled / totp_last_used_step)
backend/app/models/messaging.py             (MessageThread: add contact_id, status,
                                             assigned_user_id, last_read_at columns + indexes)
backend/app/models/__init__.py              (export new models)
backend/app/auth/security.py                (add pending-2fa token mint/verify helpers)
backend/app/api/routes/auth.py              (login branches to requires_2fa; nothing else)
backend/app/services/messaging.py           (ingestion: contact stamping + reopen-on-inbound;
                                             send path: call select_sender; keep every
                                             ingestion outcome/dedupe contract identical)
backend/app/providers/base.py               (build_carrier: loopback branch)
backend/app/main.py                         (mount the three new routers; nothing else)
backend/app/errors.py                       (add FeatureUnavailableError 503 +
                                             StickySenderUnavailableError 422 only)
backend/app/api/routes/messages.py          (SendIn: allow_reassign flag; GET /messages:
                                             `after` keyset param; nothing else)
backend/tests/conftest.py                   (add: query_counter fixture, loopback fixtures,
                                             contact/inbox helpers; P0/P1 fixtures untouched)
backend/tests/test_send_api.py              (ONLY the resolve_from_number-era assertions
                                             updated to the DR-4 sticky contract)
.github/workflows/ci.yml                    (add `frontend` job + openapi-drift step; the
                                             three existing jobs untouched)
deploy/nginx-csaas.conf                     (reference config: static root + SPA fallback +
                                             /api proxy)
deploy/deploy.sh                            (local `npm ci && npm run build`, include
                                             frontend/dist in the archive)
docs/PROGRESS.md                            (status updates only)
```

## Forbidden (implementer must never touch)

- `.env` / `.env.example` (the one flag line is Fable-applied) / any secrets, local or VPS.
- `docs/` other than `docs/PROGRESS.md`.
- All other P0/P1 modules — in particular `app/db/base.py`, `app/db/types.py`,
  `app/db/session.py`, `app/auth/deps.py`, `app/compliance/gate.py`,
  `app/providers/bandwidth/**`, `app/providers/domain.py`, `app/providers/segments.py`,
  migrations `0001`/`0002`, and every P0/P1 test file not named above. If P2 seems to
  need a change there, STOP and report.
- The ingestion contract: outcomes (DONE/RETRY/DEAD_LETTER), the idempotency
  constraint, the state-machine table, and the webhook route are frozen. P2 adds
  transactional side effects (contact stamp, reopen) *inside* the existing deduped
  transaction; it changes no outcome.
- Anything on the VPS (P2a deploys nothing; P2b is operator-supervised).
- No new backend deps beyond pyotp + cryptography; no frontend deps beyond the
  Pre-dependencies list. No MSW, no Playwright/Cypress (P6), no Redux/Zustand, no
  axios (fetch), no UI kits beyond shadcn/tailwind. No WebSockets/SSE (DR-6). No
  Docker for local frontend work.

## Implementation Notes

### 1. Schema (`models/contacts.py`, `models/messaging.py` edits, migration `0003_inbox_contacts`)

All new tables: GUID PKs (app-side uuid4), `TimestampMixin`, `TenantScoped`.

- **companies**: `name` String(255), `domain` String(255) nullable,
  `attributes` PortableJSON default dict.
- **contacts**: `display_name` String(255) NOT NULL, `first_name`/`last_name`
  String(127) nullable, `company_id` GUID FK→companies **SET NULL** nullable,
  `attributes` PortableJSON default dict. Index `(org_id, display_name)`.
- **contact_phones**: `contact_id` FK CASCADE, `e164` String(20), `label` String(31)
  default `"mobile"`, `is_primary` bool default true.
  **Unique `(org_id, e164)` — `uq_contact_phones_org_e164`** (DR-3's anchor).
- **tags**: `name` String(63), `color` String(7) default `"#64748b"`;
  unique `(org_id, name)`.
- **contact_tags**: `contact_id` FK CASCADE, `tag_id` FK CASCADE,
  unique `(contact_id, tag_id)`. (TenantScoped like everything else.)
- **thread_labels**: `thread_id` FK CASCADE, `tag_id` FK CASCADE,
  unique `(thread_id, tag_id)`.
- **contact_notes**: `contact_id` FK CASCADE, `author_user_id` GUID FK→users SET NULL
  nullable, `body` Text NOT NULL.
- **custom_field_defs**: `key` String(63) (snake_case, validated `^[a-z][a-z0-9_]*$`),
  `label` String(127), `kind` String(15) ∈ text|number|date|select, `options`
  PortableJSON default list (non-empty required iff kind=select);
  unique `(org_id, key)`.
- **message_threads** additions: `contact_id` GUID FK→contacts **SET NULL** nullable
  indexed; `status` String(8) NOT NULL server_default `'open'`; `assigned_user_id`
  GUID FK→users SET NULL nullable indexed; `last_read_at` DateTime(tz) nullable.
  New index `(org_id, status, last_message_at)`.
- **users** additions per DR-8 (global table, not tenant-scoped — unchanged truth).

Migration `0003` also (data, idempotent): backfill
`last_message_at = created_at WHERE last_message_at IS NULL`; re-seed `is_system`
roles' `permissions` from literals inlined in the migration file (owner `["*"]`, admin
= all-minus-org:delete/org:billing including the new key, agent = its list +
`inbox:manage`). No app-code imports inside the migration.

### 2. Contacts service + routes (`services/contacts.py`, `api/routes/contacts.py`)

- `resolve_or_create_contact(session, org_id, e164) -> Contact` — select via
  `contact_phones`; miss → create contact (`display_name=e164`) + phone row; on
  `IntegrityError` (concurrent inbound race) rollback → `set_org_context` → re-select.
  Same race pattern as `upsert_thread`, same test obligation.
- `link_threads_for_phone(session, org_id, e164, contact_id)` — one UPDATE over
  threads with that `contact_e164` (scoped session), stamping `contact_id`. Called on
  phone add and phone reassignment.
- Contact write API carries phones inline: `phones: [{e164, label, is_primary}]`;
  service diffs current vs submitted (add/remove/relabel), normalizes via `to_e164`,
  maps the unique-constraint violation to 409 naming the conflicting contact id.
  Attribute values validated against `custom_field_defs` (unknown key → 422; type
  mismatch → 422; select value not in options → 422).
- Endpoints (permissions in parentheses): `GET /api/v1/contacts` (contacts:read;
  `q` name/phone search, keyset by `(display_name, id)`, limit ≤100),
  `POST /api/v1/contacts` (contacts:write), `GET/PATCH/DELETE /api/v1/contacts/{id}`
  (read/write/write; DELETE → threads keep history via SET NULL),
  `GET/POST /contacts/{id}/notes` (contacts:read / contacts:write),
  `PUT /contacts/{id}/tags` (contacts:write; replaces the set),
  `GET/POST /api/v1/tags` (contacts:read / contacts:write),
  `GET/POST/PATCH /api/v1/companies` (contacts:read / contacts:write),
  `GET /api/v1/custom-fields` (settings:read), `POST/PATCH /api/v1/custom-fields`
  (settings:write; `key` immutable after creation — values already reference it).

### 3. Inbox service + routes (`services/inbox.py`, `api/routes/inbox.py`)

- `list_inbox(session, org_id, user_id, filters, cursor, limit)` implements DR-7
  exactly; returns
  `{items: [{thread, last_message, unread, contact, assignee, labels}], next_cursor}`
  where `thread` = `{id, our_e164, contact_e164, status, assigned_user_id,
  last_message_at}`, `last_message` = `{id, direction, body, status, created_at}` |
  null, `contact` = `{id, display_name}` | null, `assignee` = `{id, full_name}` | null,
  `labels` = `[{id, name, color}]`. The integration test asserts this exact key set —
  it is the frontend's contract.
- `GET /api/v1/inbox/threads` (inbox:read) — filters per DR-7.
- `PATCH /api/v1/threads/{id}` (inbox:manage) — body any of
  `{status: "open"|"closed", assigned_user_id: uuid|null}`; assignee validated
  against org membership (422 if not a member).
- `POST /api/v1/threads/{id}/read` (inbox:read) — `last_read_at = now()`; 204.
- `PUT /api/v1/threads/{id}/labels` (inbox:manage) — replace label set by tag ids.
- All resolution through the scoped session — org B's thread id is a 404 here, as in P1.

### 4. Sticky sender (`services/sender.py` + `services/messaging.py` surgical edit)

`select_sender` per DR-4. The deterministic pick is a pure function
`pick_deterministic(contact_e164: str, numbers: Sequence[str]) -> str` (sha256 mod
over sorted input) — unit-tested with pinned expected outputs so a refactor that
changes the mapping fails loudly (a mapping change re-shuffles every conversation's
affinity). `send_message` signature keeps `from_e164` (already resolved) — the route
calls `select_sender` where it called `resolve_from_number`; `SendIn` gains
`allow_reassign: bool = False`.

### 5. Ingestion additions (`services/messaging.py` surgical edit)

Inside `_ingest_inbound`'s existing single transaction, after the thread upsert:
`thread.status = "open"` (reopen-on-inbound), and
`thread.contact_id = resolve_or_create_contact(...).id` when NULL. The dedupe
`IntegrityError` path still rolls back *everything* — replays cannot double-create
contacts or re-reopen anything observable. Outbound thread creation stamps
`contact_id` the same way in `send_message`. `Outcome` semantics untouched.

### 6. Loopback carrier (`providers/loopback.py`, `providers/base.py` edit)

Per DR-2. `LoopbackCarrier(sessionmaker_getter, auto: bool = True, echo: bool = True,
dlr_delay: float = 0.5, reply_delay: float = 2.0)`. `send_message` returns accepted
and enqueues a simulation job; `auto=True` spawns an asyncio task per job (fire and
forget, errors logged, never raised into the send path); tests construct
`auto=False` and call `await carrier.drain()` for deterministic, sleep-free runs.
Simulated events are built as `DeliveryReceipt` / `InboundMessage` dataclasses with
`carrier="loopback"` provider ids and fed to `ingest_event` on a **fresh session** —
the ledger, dedupe and state machine treat them exactly like Bandwidth traffic.
`build_carrier`: loopback wins only when its flag is set (config validator has already
excluded production and the bandwidth-both-on case).

### 7. 2FA (`api/routes/twofa.py`, `auth/security.py` + `auth.py` edits)

Per DR-8. Pending token: same HS256 secret, claims `{sub, scope: "2fa-pending",
exp: +5m}`; `decode_access_token` (used by `get_current_user`) must reject any token
carrying a `scope` claim — one added guard clause, covered by a test that a pending
token on a normal endpoint is 401. Verify uses `pyotp.TOTP(...).verify(code,
valid_window=1)`; on success store the accepted timestep in `totp_last_used_step` and
reject a code whose step ≤ stored step (replay).

### 8. Frontend (`frontend/`)

Structure (keep it this small):
```
src/api/client.ts        typed fetch wrapper: base URL, Bearer + X-Org-Id headers,
                         401 → auth reset; ApiClient interface injectable for tests
src/api/types.gen.ts     GENERATED (openapi-typescript) — never hand-edited
src/api/hooks.ts         TanStack Query hooks incl. polling per DR-6
src/auth/AuthContext.tsx login/2fa/org-selection state, localStorage persistence
src/pages/LoginPage.tsx  login + requires_2fa step
src/pages/OrgPickerPage.tsx   memberships from /auth/me
src/pages/InboxPage.tsx  filter tabs (Open/Closed · Mine/Unassigned/All · label),
                         search box, ThreadList + ThreadView split pane
src/pages/ContactsPage.tsx    list/search/create/edit (drawer), tags, notes,
                              custom fields form driven by /custom-fields
src/pages/NumbersPage.tsx     list + add (existing endpoints)
src/pages/SettingsSecurityPage.tsx  2FA enroll (QR via provisioning URI) /disable
src/components/inbox/ThreadList.tsx, ThreadView.tsx, MessageBubble.tsx,
    Composer.tsx, ContactPanel.tsx, AssigneeMenu.tsx, LabelPicker.tsx
src/components/ui/*      shadcn vendored
src/lib/format.ts        phone display, relative time
```
Behaviors that must exist (each is a vitest target or a backend-contract target):
- Opening a thread fires `POST /threads/{id}/read` and zeroes its unread badge.
- Composer: optimistic append; a `201` with `status:"rejected"` renders the bubble
  with an error chip showing `error_code` (carrier rejection is data — P1 DR-7);
  a 422 `sticky_sender_unavailable` on a new conversation surfaces a "conversation
  number retired — send from a new number?" confirm that retries with
  `allow_reassign: true`.
- Message ticks: `queued/accepted` ○, `sending` ◔, `delivered` ✓, `failed/rejected`
  ✗ + code tooltip — driven purely by polled status.
- AssigneeMenu: member list when `GET /orgs/current/members` succeeds; on 403 it
  degrades to Assign to me / Unassign (DR-9).
- New-conversation modal takes contact-or-phone only; the server sticky-picks the
  sender.

### 9. OpenAPI drift + CI (`scripts/export_openapi.py`, `ci.yml` edit)

`export_openapi.py`: build the app via `create_app(make-test-ish settings)`, dump
`app.openapi()` JSON (sorted keys) to `frontend/openapi.json`. npm scripts:
`gen:api` = export (via `python`) + `openapi-typescript openapi.json -o
src/api/types.gen.ts`; `typecheck` = `tsc --noEmit`; `test` = `vitest`.
New CI job `frontend` (Node 22): `npm ci` → regenerate openapi.json + types →
`git diff --exit-code frontend/openapi.json frontend/src/api/types.gen.ts` →
`npm run typecheck` → `npm test -- --run` → `npm run build`. The three existing jobs
are untouched; **`test-postgres` remains the merge gate, now joined by `frontend`.**

### 10. Explicitly NOT in P2 (scope fence — reject any of these in review)

- No MMS/media rendering or upload, no templates/merge fields, no compliance logic —
  STOP/quiet-hours/DNC/consent are P3 (the seam sits untouched; no
  `do_not_contact` flag on contacts either, P3's ledger owns that state).
- No number search/order/10DLC (P4) — Numbers page uses the P1 hand-entry endpoint.
- No voice anything (P5+). No WebSockets/SSE/push (DR-6; P13).
- No campaigns, bulk send, scheduling, drip, segments, CSV import/export,
  contact merge UI or endpoint (P11).
- No snoozed status, per-user read cursors, private thread notes, canned responses,
  CSAT, per-message tags (P13; DR-5).
- No `number_pools` table (DR-4), no per-number opt-out state of any kind.
- No backup codes / remember-device / SMS 2FA (DR-8). No password reset / email /
  invites (still parked; the invite flow needs SMTP — revisit P4).
- No Playwright/Cypress/browser E2E (P6). No Storybook. No i18n. No dark mode work
  beyond what shadcn defaults give free.
- No new FastAPI middleware, no rate limiting, no API keys (P13).
- Loopback carrier never ships enabled in any prod config; it is a dev/demo tool.

## Test Spec

Backend — all local commands from `backend/` in the venv, on this Windows machine, today:

```
python -m pytest -q          # SQLite; pg_only + live_carrier skipped — must be green
python -m ruff check .       # dialect ban + import hygiene — must be clean
```

Frontend — from `frontend/`, Node 22, today:

```
npm ci
npm run gen:api && git diff --exit-code openapi.json src/api/types.gen.ts
npm run typecheck            # tsc --noEmit, strict
npm test -- --run            # vitest, jsdom
npm run build                # production build must succeed
```

Backend unit tests:
- [ ] `test_sticky_sender.py::test_pick_deterministic_pinned` → fixed inputs
      (3 numbers, 4 contacts) produce the exact pinned mapping; permuting the input
      order of numbers does not change the mapping (sorted-pool property).
- [ ] `test_sticky_sender.py::test_sticky_reuses_thread_number` → org with numbers
      A,B; existing thread contact↔A; send with no `from` → uses A even when the
      deterministic pick would say B.
- [ ] `test_sticky_sender.py::test_inactive_sticky_fails_loudly` → deactivate A →
      send no-`from` → 422 `sticky_sender_unavailable`, zero rows, zero carrier
      calls; same send with `allow_reassign:true` → 201 from B, a NEW thread bucket,
      and a WARNING `sticky_sender_reassigned` captured.
- [ ] `test_sticky_sender.py::test_explicit_from_still_enforced` → other-org number
      or inactive number as explicit `from` → 422 (P1 teeth preserved).
- [ ] `test_twofa.py::test_enroll_activate_login_verify` → enroll → activate with
      pyotp-generated code → login returns `requires_2fa` + pending token → verify →
      real token works on `/auth/me`.
- [ ] `test_twofa.py::test_pending_token_is_not_an_access_token` → pending token on
      `/auth/me` → 401.
- [ ] `test_twofa.py::test_code_replay_rejected` → same code twice → second is 401;
      wrong code → 401; `totp_secret` in DB is Fernet-encrypted (not the base32).
- [ ] `test_twofa.py::test_enroll_without_fernet_key_503` → settings without
      `CREDENTIAL_ENCRYPTION_KEY` → 503 `feature_unavailable`.
- [ ] `test_loopback_carrier.py::test_production_guard` → Settings with
      `app_env=production` + loopback flag → `ConfigurationError`; ditto
      loopback + `bandwidth_enabled=true`.

Backend integration tests (httpx `AsyncClient`, SQLite):
- [ ] `test_contacts.py::test_crud_and_phone_uniqueness` → create contact with
      phone; second contact claiming the same phone → 409 naming the holder;
      PATCH moves the phone → threads re-linked (`link_threads_for_phone`).
- [ ] `test_contacts.py::test_inbound_autocreates_and_links` → inbound webhook from
      unknown number → contact exists (`display_name` = E.164), thread stamped;
      **replaying the identical webhook creates no second contact** (the dedupe
      transaction covers the side effects).
- [ ] `test_contacts.py::test_late_contact_adoption` → thread + messages exist with
      NULL contact; create a contact with that phone → thread's `contact_id` stamped;
      `scripts/backfill_contact_links.py` logic is a no-op second time (idempotent).
- [ ] `test_contacts.py::test_custom_field_validation` → def kind=number rejects
      `"abc"` (422); unknown attribute key → 422; select outside options → 422;
      valid write round-trips.
- [ ] `test_contacts.py::test_tenancy` → org B sees none of org A's contacts,
      tags, notes, companies, custom-field defs (list empty + direct id 404).
- [ ] `test_inbox_aggregate.py::test_shape_contract` → seeded inbox; response items
      carry EXACTLY the DR-7/§3 keys; previews are each thread's true latest
      message; unread matches derived counts.
- [ ] `test_inbox_aggregate.py::test_query_count_constant` — **THE N+1 GATE**: with
      the `query_counter` fixture, 30 threads × 5 messages;
      `count(limit=25) == count(limit=5)` AND `count ≤ 8`.
- [ ] `test_inbox_aggregate.py::test_keyset_pagination_stable` → walk 3 pages by
      cursor: no duplicates, no gaps; inserting a new thread mid-walk does not
      corrupt subsequent pages (keyset property, the reason offset lost).
- [ ] `test_inbox_aggregate.py::test_filters` → status/assigned=me/unassigned/label/
      `q` by contact name and by phone fragment each return exactly the expected
      thread ids.
- [ ] `test_thread_state.py::test_assign_close_read` → PATCH assign (member ok,
      non-member 422), close; `POST read` zeroes unread; new inbound after read →
      unread 1 again, and **thread reopens** (`status=="open"`).
- [ ] `test_thread_state.py::test_reopen_replay_safe` → close thread; deliver the
      SAME inbound webhook twice → one message, thread open — replay changed nothing
      twice.
- [ ] `test_thread_state.py::test_inbox_manage_permission` → role without
      `inbox:manage` → PATCH/labels 403; agent (post-migration seed) → 200.
- [ ] `test_loopback_carrier.py::test_full_simulated_conversation` → app wired with
      `LoopbackCarrier(auto=False)`; POST /messages → `accepted`; `await drain()` →
      status `delivered` (ledger holds loopback sending+delivered rows) and an
      inbound echo message exists in the SAME thread; thread unread == 1; all of it
      via the real ingestion service (assert `message_events` rows exist —
      proof it did not bypass the ledger).
- [ ] `test_send_api.py` (edited) → prior ambiguous-from assertions now assert the
      sticky contract; every other P1 assertion untouched and green.
- [ ] (`pg_only`) `test_inbox_aggregate.py::test_aggregate_on_postgres` → shape +
      query-count tests re-run on PG (window function + keyset parity).

Frontend tests (vitest, jsdom — stubbed ApiClient):
- [ ] `client.test.ts` → attaches `Authorization` + `X-Org-Id`; 401 response clears
      stored auth and signals logout.
- [ ] `login.test.tsx` → happy login stores token; `requires_2fa` response renders
      the code step and calls `2fa/verify` with the pending token.
- [ ] `inbox.test.tsx` → threads render preview, contact name, unread badge, label
      chips; filter tab switch triggers the right query params.
- [ ] `composer.test.tsx` → send renders optimistic bubble; `status:"rejected"`
      201 renders the error chip with `error_code`;
      `sticky_sender_unavailable` 422 renders the reassign confirm and resends
      with `allow_reassign:true`.
- [ ] `threadview.test.tsx` → opening a thread calls the read endpoint; a poll
      response containing a new inbound appends it without duplicating existing
      messages (keyed by id).

Manual verification:
- [ ] P2a demo: backend `uvicorn` with `LOOPBACK_CARRIER_ENABLED=true` + dev Fernet
      key; `npm run dev`; log in, create a contact with a phone, message it from the
      inbox → watch ticks reach `delivered` and the echo reply arrive by polling;
      assign, label, close, reopen-by-reply; enroll 2FA and complete a 2FA login.
- [ ] CI: all five jobs green (`lint`, `test-sqlite` ×2, `test-postgres`,
      `frontend`); `test-postgres` + `frontend` are the merge gates.
- [ ] P2b (operator, post-R1): deploy per below; from a phone on LTE (not the office
      network) log in over TLS and run a real conversation with a handset;
      `scripts/backfill_contact_links.py` run once on the box; PROGRESS updated.

Pass criteria — **P2a:** every backend + frontend check above green locally (3.10 +
SQLite / Node 22) and in CI; ruff clean; loopback demo performed. Commit
`feat(phase-2a): contacts, conversation state, sticky sender, unified inbox console`.
**P2b:** the live browser↔handset conversation demonstrated on the deployed console.
Commit `feat(phase-2b): console live on VPS — browser SMS round-trip verified`.
P3 may start on P2a sign-off (it consumes the seam and models, not the live carrier).

## Deploy

**P2a: no.** Nothing new on the VPS.
**P2b: yes — operator-supervised, after/with P1b.** `deploy.sh` builds the frontend
locally (Node 22) and ships `frontend/dist`; nginx site file (from
`deploy/nginx-csaas.conf`) serves the static root with SPA fallback and proxies
`/api/` + `/healthz` to 127.0.0.1:8080 — this widens exposure from P1b's
webhook-only path to the full API, which is why 2FA ships in this phase and not
later. `alembic upgrade head` runs `0003` (additive + idempotent data fixes);
`scripts/backfill_contact_links.py` runs once. All P0 VPS constraints restated:
everything under `/opt/csaas`, compose project `csaas`, api on 127.0.0.1:8080 only,
no global installs, self-contained nginx site file only, pre-flight before anything
starts, `LOOPBACK_CARRIER_ENABLED` absent/false in the prod `.env` (boot refuses it
anyway).
