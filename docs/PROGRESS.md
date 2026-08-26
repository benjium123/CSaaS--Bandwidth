# PROGRESS — start here in every new session

> **This file is the resume point.** Read it first. Update it at the end of every phase,
> and whenever a decision or a blocker changes. If it disagrees with your memory, this file
> is right.

**Last updated:** 2026-08-26 — P3 compliance core
**Current phase:** P3 — **compliance core done (199 backend tests green, CI green).**
Media pipeline + routes + frontend still to do. P1b/P2b/P3b blocked on R1.
**Current blocker:** R1 — confirm Bandwidth account path to production

### P0 remaining before sign-off (all need the user, not more code)
1. **R1** — confirm the Bandwidth account reaches production. Blocks Track R and P1.
2. ~~CI verification~~ — ✅ **DONE.** All 4 jobs green on `5c73dc9`: lint, test-sqlite (3.10),
   test-sqlite (3.12), and **test-postgres** (the merge gate) — which ran
   `alembic upgrade head / downgrade base / upgrade head` on real Postgres 16 and passed the
   full suite including the `pg_only` tenant-isolation test.
3. **VPS deploy** — **deliberately not executed autonomously.** The box runs live
   production services for other businesses. `deploy/deploy.sh` is written, pre-flight
   guarded and idempotent, but it requires (a) Docker present on the box and (b) the
   operator to create `/opt/csaas/.env` by hand. Run it when you're ready to watch it.

---

## Read-first order for a new session

1. This file
2. `docs/ARCHITECTURE.md` — the settled decisions (do not relitigate)
3. `docs/PHASES.md` — what phase you are on
4. `docs/plans/phase-N-plan.md` — the active phase plan, if one exists
5. `docs/research/*.md` — only the one relevant to what you are building

---

## Track R — Brand & Campaign Registration (parallel, highest-priority non-code task)

Full detail and the live status table: **`docs/BRAND_REGISTRATION.md`**.

Registration is a wall-clock wait of days-to-weeks. The **first brand + campaign are
submitted manually via the Bandwidth dashboard NOW, in parallel with P0** — P4 only
automates it for future brands. Toll-free verification is a separate **3–6 week** track.
Unregistered numbers get error `4476` and are rejected, not queued.

**Track R status: 🔴 blocked on R1** (Bandwidth account path to production).

---

## Phase status

| Phase | Name | Status | Gate passed | Deployed | Commit |
|---|---|---|---|---|---|
| P0 | Foundation | 🔵 in review | local ✅ / CI+PG ✅ / VPS ⬜ | ⬜ pending | `5c73dc9` |
| P1a | Carrier layer + ingestion (fixtures) | 🔵 in review | local ✅ / CI+PG ✅ | n/a | `d6b5f59` |
| P1b | Live SMS round-trip | 🔴 blocked on R1 | — | — | — |
| P2a | Contacts, inbox console, sticky sender, 2FA | 🔵 in review | local ✅ / CI+PG ✅ | n/a | `798245b` |
| P2b | Console live on VPS | 🔴 blocked on R1 | — | — | — |
| P3-core | Compliance: opt-out, keywords, quiet hours, DNC | 🔵 in review | local ✅ / CI+PG ✅ | n/a | `69f9278` |
| P3-rest | MMS media pipeline, routes, templates | 🔵 in review | local ✅ / CI+PG ✅ | n/a | `96d3dc8` |
| P3b | Carrier routing fabric (pulled fwd from P14) | 🔵 in review | local ✅ (252) | n/a | `ffd370f` |
| P4 | Numbers + 10DLC + TFV | 🔵 in review (backend) | local ✅ (273) | 🔴 blocked on R1 | — |
| P5 | Voice core | 🔵 in review | local ✅ (354) | 🔴 blocked on R1 + trunk | — |
| P6 | LiveKit media plane + softphone | 🔵 in review | local ✅ (405) | 🔴 needs trunk + VPS bring-up | — |
| P7 | Media plane measured + echo agent | 🔵 code complete | metrics 6/6 local | 🔴 gate runs on VPS | — |
| P8 | AI voice agent v1 | ⬜ not started | — | — | — |
| P9 | AI voice agent v2 | ⬜ not started | — | — | — |
| P10 | AI SMS agent | ⬜ not started | — | — | — |
| P11 | Outbound engine | ⬜ not started | — | — | — |
| P12 | IVR / queues / voicemail | ⬜ not started | — | — | — |
| P13 | Analytics + platform services | ⬜ not started | — | — | — |
| P14 | Failover + hardening | ⬜ not started | — | — | — |

Status values: ⬜ not started · 🟡 in progress · 🔵 in review · ✅ gate passed · 🔴 blocked

---

## Workstream status

| WS | Workstream | Status | Notes |
|---|---|---|---|
| WS-0 | Foundation | 🔵 code complete — tenancy, RBAC, settings, auth, errors, logging | |
| WS-1 | Carrier Abstraction Layer | 🔵 CAL + Bandwidth messaging adapter + webhook ingestion | |
| WS-2 | Messaging | 🔵 messages/threads/events schema, state machine, send + read API | |
| WS-3 | Voice Core | ⬜ | |
| WS-4 | Media & AI Voice Agent | ⬜ | |
| WS-5 | AI SMS Agent | ⬜ | |
| WS-6 | Outbound Engine | ⬜ | |
| WS-7 | Console | 🔵 React+Vite+TS inbox, contacts, numbers, security pages | |
| WS-8 | Compliance | 🔵 gate filled in: ledger, keywords, quiet hours, DNC, auto-replies | |
| WS-9 | Platform Services | ⬜ | |
| WS-10 | DevOps | 🔵 CI + Dockerfile + compose + deploy.sh written; deploy not yet run | |

---

## Open blockers and risks

| # | Risk | Status | Resolve by |
|---|---|---|---|
| R1 | **Bandwidth account path to production.** Historically sales-gated, no public pricing. "Bandwidth Build" (2026-06-23) is self-serve but it's unverified whether the trial scales to production without a contract. | 🔴 **open — blocks P0** | P0 |
| R2 | VPS ↔ Bandwidth media PoP may be long-haul → TCP dead air (we measured 300–650 ms/sec on a prior long-haul TCP audio leg) | ⬜ open | P7 gate, before building |
| R3 | Deepgram Nova-3 real WER on our 8 kHz caller audio (marketed 5.26% vs 12–25% independent) | ⬜ open | P8 bake-off |
| R4 | Two live Bandwidth doc generations — legacy `v2.dev.` / `old.dev.` / `catapult` endpoints look authoritative in search | 🟡 mitigated by standing rule | ongoing |
| R5 | 10DLC + TFV vetting takes days–weeks; throughput is Trust-Score-driven, not flat 1 MPS | ⬜ open | start P4 early |
| R6 | `<StartStream>` frame size undocumented; DTMF does **not** arrive over the media WS | ⬜ open | P7 |
| R7 | Bandwidth conference caps (20 participants / 24 h / 6 verbs) and **you cannot StartStream a conference room** | ⬜ open | P12 design |
| R8 | **Local Python is 3.10.10; Pipecat needs 3.11/3.12.** Also no Docker and no Postgres on the dev machine. | 🟡 known, planned around | Python upgrade by **P7**; test strategy in `docs/plans/phase-0-plan.md` |

---

## Decision log

Decisions live in `docs/ARCHITECTURE.md`. This is the index with dates.

| Date | ID | Decision |
|---|---|---|
| 2026-08-26 | D1 | Hybrid transport — AI on Bandwidth bidirectional WS media, humans on WebRTC. Not building a SIP stack for latency (transport is 3–15% of the budget). |
| 2026-08-26 | D2 | Carrier abstraction modelled Telnyx-shaped (event in → async command out); Bandwidth adapter serializes down to BXML. |
| 2026-08-26 | D3 | FastAPI + Postgres + Redis + React/Vite/TS/Tailwind/shadcn. **Amended at P0 (DR-5): we do NOT fork `full-stack-fastapi-template`** — its Docker-first loop, superuser/user binary and bundled frontend are exactly what we'd delete. Layout used as reference; ~30 lean files written instead. |
| 2026-08-26 | D9 | **Tenant isolation is enforced at the SQLAlchemy session layer**, not by query discipline: a `do_orm_execute` hook injects `org_id` into every tenant-scoped read and *raises* when no org context is set; a `before_flush` hook blocks cross-tenant writes. Forgetting the filter is impossible, not merely unlikely. |
| 2026-08-26 | D10 | **Local tests run on SQLite, Postgres CI is the merge gate.** Three guards stop Postgres-only SQL slipping past the local suite: portable `GUID`/`PortableJSON` types, a Ruff banned-api rule on `sqlalchemy.dialects.postgresql` outside `db/types.py` + `migrations/`, and a strict `pg_only` marker. |
| 2026-08-26 | D11 | **2FA moved P0 → P2** (SPEC deviation). With no console there is no enrolment surface, so P0 2FA would be untestable end-to-end. |
| 2026-08-26 | D4 | Cascaded STT→LLM→TTS, not speech-to-speech. p50 ≤ 700 ms target. |
| 2026-08-26 | D5 | Audio-pipeline law carried forward: shed only per-frame-silent frames; `rt=1.0` proves nothing; conversation-replay test gates audio commits. |
| 2026-08-26 | D6 | All webhook handlers idempotent + state-based. Both carriers retry unordered. |
| 2026-08-26 | D7 | Compliance is a first-class module in the send path. No OSS library exists for TCPA/DNC or 10DLC. |
| 2026-08-26 | D8 | Not forking Chatwoot / Dograh / jambonz. No AGPL or Commons-Clause in the core. |
| 2026-08-26 | D12 | **Multi-carrier routing moved P14 → P3b.** A DID belongs to exactly one carrier, so routing picks a (number, carrier) PAIR and a carrier switch changes the sender the recipient sees. Cross-carrier failover is therefore opt-in and refused mid-thread; intra-carrier is the default. Explicit carrier choices are honoured or refused, never substituted. |
| 2026-08-26 | D13 | **Sinch not adopted.** Its edge is global reach, which is irrelevant to a US-only 10DLC/TFV footprint; its API surface is fragmented across acquisitions and its media-streaming story is the weakest of the four. The adapter would be ~150 lines if that changes — the registry makes this cheap to revisit, which is the point. |
| 2026-08-26 | D17 | **One media plane: LiveKit for BOTH the AI agent and the browser softphone** (P6 spike resolved). D1's revisit condition 1 fired — warm transfer / supervisor listen-in / AI↔human handoff are first-class requirements, and if both endpoints are room participants those are room operations instead of cross-vendor carrier transfers. PSTN via livekit-sip + carrier SIP trunks, so carriers stay swappable at the trunk level (P3b's whole point). LiveKit is infrastructure, NOT a carrier — it never appears in routing. Named fallback: if the P7 measurement gate fails on the VPS and region-pinning doesn't cure it, softphone falls back to Telnyx WebRTC SDK. Plans: phase-6-plan.md / phase-7-plan.md. |
| 2026-08-26 | D15 | **Registration is a PRE-SEND gate, not a post-send carrier error.** A number linked to a campaign/TFV that is not `approved` cannot be selected or sent from. Numbers we hold NO registration for are still allowed (they are often registered directly at the carrier) and logged — we refuse what we know is wrong, never what we merely do not know. `REQUIRE_NUMBER_REGISTRATION` can only make this stricter. |
| 2026-08-26 | D16 | **Bandwidth number provisioning deliberately NOT implemented.** IRIS ordering needs a SiteId/SipPeerId this account has not been issued, on credentials that return 401 (R1). `as_provider()` raises a clear FeatureUnavailableError telling the operator to add the number by hand; Telnyx provisioning is implemented and tested. Writing an unverifiable XML integration would produce code that looks finished and fails on first contact. |
| 2026-08-26 | D14 | **AI voice agents will run on LiveKit Agents** (user directive). This is D1's stated revisit condition firing: LiveKit reaches PSTN via LiveKit SIP + a carrier SIP trunk, so it buys native multi-party rooms (warm transfer, conferencing, supervisor barge) and a mature turn-detector, at the cost of a SIP leg and an SFU to run. It does NOT overturn D1's finding that transport is 3–15% of the latency budget — the reason to adopt it is capability, not speed. Re-plans P5/P7/P8. |

---

## Rejected approaches — do not re-propose without new evidence

| Rejected | Why |
|---|---|
| Own SIP stack (Kamailio/RTPengine/FreeSWITCH) **for AI latency** | Transport is 3–15% of the voice-to-voice budget. Endpointing alone is a bigger lever. Revisit only for warm transfer / conferencing / supervisor whisper / multi-carrier failover. **Superseded in part by D14**: we adopt LiveKit (which uses a SIP trunk) for those capabilities — still NOT for latency, and still not a stack we build ourselves. |
| Building E911 / per-seat PBX ("Dialpad clone") | Explicitly dropped by the user. E911 is mandatory for a business phone system with seats; it is not required for AI outbound/inbound conversations, and skipping it removes a real regulatory burden. |
| Speech-to-speech (OpenAI Realtime / Gemini Live) as the primary | Black box: no transcript boundary for compliance, can't inject business rules mid-turn, can't swap the LLM, costs more. |
| Forking Chatwoot | Rails + Vue. Harvest the data model instead. |
| Forking Dograh wholesale | Voice-only. Forking makes our core someone else's AI-voice product with SMS bolted on. |
| jambonz as the core | v10+ requires a paid license keyed to your DNS domain. |
| Vocode | Dead — last release Jun 2024. |
| Headless-browser webphone for human agents | Measured 0.68× audio render under call load; no hardware clock under Xvfb. |
| RCS in v1 | Bandwidth A2P RCS at scale is "coming soon". |
| Per-number opt-out lists | STOP must suppress the whole pool. Per-number is a bug. |

---

## Session handoff template

Copy this at the end of a session:

```
### Session YYYY-MM-DD
Phase: P_
Did:
Tests:  (command + actual output, not "should pass")
Files changed:
Open decisions:
Next step:
Blockers:
```

---

## Session log

### Session 2026-08-26 — planning + P0 implementation
**Phase:** P0

**Did:**
- Full plan: ARCHITECTURE (D1–D11), SPEC, PHASES (P0–P14), WORKSTREAMS (WS-0..10),
  BRAND_REGISTRATION (Track R), DELEGATION, COSTS, and 7 research docs.
- Fable refined `docs/plans/phase-0-plan.md` before implementation (DR-1..DR-6).
- Implemented P0: settings validation + secret redaction, structured logging + error
  taxonomy, orgs/users/roles/memberships schema, session-level tenant isolation,
  argon2id + PyJWT auth, RBAC with a real deny path, `/healthz`, Alembic migration,
  3-job CI, Dockerfile + compose + guarded `deploy.sh`.

**Tests (actual output):**
```
$ python -m pytest -q
...............................s                    [100%]
31 passed, 1 skipped in 2.87s          # the skip is pg_only, by design

$ python -m ruff check .
All checks passed!

$ DATABASE_URL=postgresql+asyncpg://... alembic upgrade head --sql
CREATE TABLE orgs ( id UUID NOT NULL, ... )   # native UUID on PG — GUID variant works

$ curl -i http://127.0.0.1:8099/healthz
HTTP/1.1 200 OK   x-request-id: 9366c0f5-...
{"status":"ok","env":"development","version":"0.1.0","db":"ok"}
```

**Verified by hand:** provider report prints missing VARIABLE NAMES only, no values.

**Gotcha found:** OS environment variables override `.env`. A provider can report
`enabled=true` with a blank `.env` line if the var is exported in the shell (DEEPSEEK_API_KEY
is, on this machine). Correct precedence, surprising output — documented in README.

**Next step:** P1 — carrier layer + first SMS round-trip. Have Fable refine
`phase-1-plan.md` first. **P1 needs R1 answered** (a real Bandwidth account) to reach its
gate; the adapter and webhook-ingest code can be built and unit-tested against fixtures
before that.

**Blockers:** R1 (Bandwidth account → production). VPS deploy not run — deliberately left
for a supervised run since the box is production for other businesses.

---

### Session 2026-08-26 (cont.) — P1a
**Phase:** P1a (Fable refined `docs/plans/phase-1-plan.md` first, splitting P1 around R1)

**Did:** Carrier Abstraction Layer (`MessagingCarrier` protocol + frozen domain objects),
Bandwidth messaging adapter (direct REST, no SDK), pure webhook parse/verify, segment
estimator, 5-table messaging schema with a DB-constraint idempotency ledger, monotonic
message state machine, send + read + numbers API, compliance seam, migration `0002`,
nginx reference config, live-carrier suite and smoke script.

**Tests (actual output):**
```
$ python -m pytest -q
99 passed, 4 skipped in 9.61s     # skips = 2 pg_only + 2 live_carrier, all by design
$ python -m ruff check .
All checks passed!
$ alembic upgrade head --sql      # renders uq_msg_events_dedupe, uq_messages_provider_id, ...
```

**Mutation-tested the gate rather than trusting a first-run pass.** Removing the
monotonic-rank guard → unit tests fail. Removing terminal-immutability → **everything still
passed.** Cause: every terminal status has rank 30, so `rank(new) <= rank(current)` already
blocks equal-rank terminals — the two guards deliberately overlap. Removing BOTH fails 10
tests including both integration gates, so the tests are not vacuous. Added
`test_conflicting_terminal_does_not_overwrite` to cover `failed`-after-`delivered`
explicitly.

**Design notes worth remembering:**
- A DLR for an unknown message id returns **500 on purpose** — the webhook can beat our own
  commit, and Bandwidth's 24 h retry heals the race. Silently 200-ing would drop real deliveries.
- Idempotency is a **unique constraint**, not an application check: Bandwidth publishes no
  event id and retries in parallel, so only the DB is safe.
- Carrier rejection returns **HTTP 201 with `status="rejected"`**, not an HTTP error.

**Next step:** P2 (contacts + conversations + inbox) can start on P1a — it consumes the
models and API, not the live carrier. P1b completes whenever R1 does.

**CI:** all 4 jobs green on `d6b5f59`. The Postgres job matters most here — it is the only
one that runs `test_concurrent_duplicate_ingest_pg`, proving the IntegrityError dedupe path
holds under TRUE parallelism. SQLite serializes writes and cannot exercise it.

**Blockers:** R1 unchanged.

---

### Session 2026-08-26 (cont.) — P2a
**Phase:** P2a (Fable refined `docs/plans/phase-2-plan.md` first)

**Did:** contacts/companies/tags/notes/custom-fields, conversation state (open/closed,
assignment, derived unread, labels), sticky sender, the inbox aggregate with a hard N+1
gate, TOTP 2FA, a dev-only loopback carrier, migration `0003`, and the first frontend —
React + Vite + TS console (login/2FA, org picker, inbox, contacts, numbers, security).

**Tests (actual output):**
```
backend : 139 passed, 5 skipped   (ruff clean)
frontend: 17 passed, 3 files      (tsc --noEmit clean, vite build clean)
gen:api : regeneration is byte-identical across runs (drift gate is real)
```

**THREE REAL BUGS the tests caught, all of which Postgres would have hidden:**

1. **Pagination cursor corrupted by hours.** SQLite returns NAIVE datetimes for
   `DateTime(timezone=True)`; Postgres returns aware ones. `encode_cursor` called
   `.astimezone(utc)` on the naive value, which Python interprets as LOCAL time — so the
   cursor shifted by the machine's UTC offset and page 2 matched nothing. Fixed by
   stamping naive values as UTC instead of converting them (`_as_utc`), plus `_bind_dt`
   so the bound value matches what each backend actually stores.
2. **SQLite silently ignores foreign keys.** `ON DELETE SET NULL` did nothing locally while
   working on Postgres — the local suite was passing on referential behaviour it never
   exercised. `PRAGMA foreign_keys=ON` is now set on the test engine.
   ⚠ **Note for dev:** the app itself does not set this pragma, so anyone running the app
   on SQLite locally still gets unenforced FKs. Production is Postgres, so this is a
   dev-fidelity gap, not a prod risk — but do not trust local FK behaviour without it.
3. **`Badge` dropped `aria-label`**, so the unread count had no accessible name.

**Design notes worth remembering:**
- **Loopback carrier** makes the console demoable with no Bandwidth account. It drives the
  REAL ingestion service; a ledger assertion proves it does not bypass it. Boot refuses it
  in production and refuses it alongside `BANDWIDTH_ENABLED`.
- **Sticky sender never silently jumps.** A retired number fails 422 unless the caller
  passes `allow_reassign`. The deterministic pick is pinned by test — changing the hash
  would reshuffle every conversation's affinity.
- **Unread is derived from a `last_read_at` cursor, never counted.** A counter incremented
  from a webhook handler is the exact side effect D6 bans, and it would drift on replay.
- **Polling, not WebSockets** (plan DR-6). Freshness is isolated in `src/api/hooks.ts`, so
  swapping transports later touches one file. P7's media WebSocket is carrier frames with
  a hard latency budget — UI fan-out must not be multiplexed onto it.
- **OpenAPI drift gate** replaces the missing browser E2E layer until Playwright lands in
  P6: CI regenerates `openapi.json` + `types.gen.ts` and fails on any diff.

**Recorded deviations:** P1's ambiguous-from 422 became the sticky contract (plan DR-10);
per-message tags deferred to P13 in favour of thread-level labels (DR-5).

**CI:** all 5 jobs green on `798245b` — `lint`, `test-sqlite` (3.10 + 3.12),
**`test-postgres`** and **`frontend`** (the two merge gates).

**One Postgres-only failure worth remembering:** the first P2a run failed the Postgres gate
because a SQLite capability check (`select sqlite_version()`) ran on Postgres. Nothing else
failed there — every `pg_only` test passed first time.

**Reading CI failures:** GitHub refuses job-log downloads via the API without admin rights
even on a public repo, so the `test-postgres` job now re-emits the pytest summary as
workflow **annotations**, which ARE readable unauthenticated:
`GET /repos/{owner}/{repo}/commits/{sha}/check-runs` → each run's `annotations_url`.
Use that instead of trying to fetch logs.

**Next step:** P3 (MMS + compliance core) can start on P2a — it consumes the seam and the
models. P2b needs R1.

---

### Session 2026-08-26 (cont.) — P3 compliance core
**Phase:** P3 (Fable refined `docs/plans/phase-3-plan.md` first)

**Did:** append-only consent ledger, whole-message keyword engine, recipient-timezone quiet
hours with DST, internal DNC + scrub, honest federal-DNC stub, compliance audit ledger,
auto-replies, defer/hold + `release_held_messages`, `ObjectStore` seam (LocalFS +
InMemory), template renderer, migration `0004`. **199 backend tests green; all 5 CI jobs
green including Postgres.**

**CARRIER REALITY CHANGED — READ THIS BEFORE PLANNING P4/P5.**
The Bandwidth account (ID **9903389**, Bandwidth Build, 3,000 credits) is **Voice + Numbers
only — no Messaging**. The user is registering 10DLC on **Telnyx** for SMS. So the real
shape is **Telnyx for SMS, Bandwidth for voice**, inverting the plan's assumption.
Consequences:
- **P4 (10DLC) and P1b (live SMS) cannot be validated on the Bandwidth account.**
- **A Telnyx messaging adapter moves from P14 to next-up** — it is the only way to
  validate any SMS work end to end. The CAL was deliberately modelled Telnyx-shaped
  (D2), so this is a new adapter file, not a refactor.
- **P5 (voice) is what the Bandwidth account CAN validate.**
Recommended order from here: **Telnyx messaging adapter → P5 voice → P4 numbers/10DLC.**
Not yet applied to PHASES.md — awaiting the user's call.

**Bandwidth API credentials do NOT authenticate.** Client ID `CLI-6a83e246-…` + secret are
in `.env`, but all 8 probes return 401 (OAuth2 with/without scope, header and body creds,
Dashboard/IRIS, Messaging, Voice, and reversed ID/secret). `id.bandwidth.com` returns a
Bandwidth-shaped error, so they are recognised and rejected — most likely the secret needs
regenerating (Account → API Credentials).

**Domain:** `sabinepropertygroup.net` → 145.79.24.121 and `www` → 145.79.24.167 (the
website host), **not** the CSaaS VPS. Webhook URLs must use a new `api.` subdomain
A-record'd to 144.126.152.175. Until deployed, use webhook.site for carrier webhook fields.

**DELEGATION FIX worth keeping:** `claude_tools/delegate.py` caps DeepSeek at 4000
max_tokens. V4 Pro is a REASONING model whose reasoning tokens come out of that same
budget, so on any large file it burns the budget thinking and returns an **empty string
with exit code 0** — which is why DeepSeek looked useless. A wrapper that requests a large
budget, checks `finish_reason`, and fails loudly lives in the session scratchpad
(`dspro.py`, with `pro`/`flash` tiers). Observed: a big factual table made Pro spend
16000/16000 tokens on reasoning and emit nothing — reasoning models are the wrong tool for
bulk factual recall; use Flash for mechanical work.

**Two seam-contract saves.** The frozen `test_compliance_seam.py` caught both: passing
`exemption=` unconditionally, and adding a 4th positional arg to `on_inbound`. P1/P2's
three-argument spies must keep working, so the fix was conditional kwargs and routing the
carrier through `session.info` — *not* editing the tests, which would have been editing
the evidence.

**Still to do in P3:** media pipeline (upload, signed URLs, `fetch_pending_media` outside
the webhook path), sweeper loop, API routes (compliance/media/templates), and the frontend
surfaces (attachments, opt-out banner, compliance page).
