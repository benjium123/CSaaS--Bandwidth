# PROGRESS — start here in every new session

> **This file is the resume point.** Read it first. Update it at the end of every phase,
> and whenever a decision or a blocker changes. If it disagrees with your memory, this file
> is right.

**Last updated:** 2026-08-26 — P0 implementation
**Current phase:** P0 — **code complete, local + CI/Postgres green.** Awaiting R1 + VPS deploy.
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
| P1 | Carrier layer + first SMS | ⬜ not started | — | — | — |
| P2 | Contacts / conversations / inbox | ⬜ not started | — | — | — |
| P3 | MMS + compliance core | ⬜ not started | — | — | — |
| P4 | Numbers + 10DLC + TFV | ⬜ not started | — | — | — |
| P5 | Voice core | ⬜ not started | — | — | — |
| P6 | Browser softphone | ⬜ not started | — | — | — |
| P7 | Media streaming + echo bot | ⬜ not started | — | — | — |
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
| WS-1 | Carrier Abstraction Layer | ⬜ | |
| WS-2 | Messaging | ⬜ | |
| WS-3 | Voice Core | ⬜ | |
| WS-4 | Media & AI Voice Agent | ⬜ | |
| WS-5 | AI SMS Agent | ⬜ | |
| WS-6 | Outbound Engine | ⬜ | |
| WS-7 | Console | ⬜ | |
| WS-8 | Compliance | ⬜ | |
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

---

## Rejected approaches — do not re-propose without new evidence

| Rejected | Why |
|---|---|
| Own SIP stack (Kamailio/RTPengine/FreeSWITCH) **for AI latency** | Transport is 3–15% of the voice-to-voice budget. Endpointing alone is a bigger lever. Revisit only for warm transfer / conferencing / supervisor whisper / multi-carrier failover. |
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
