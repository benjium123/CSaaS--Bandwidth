# Phase 14 — Failover, hardening, production

**WS:** 1, 8, 10 · **Deploy:** yes · **Planned:** 2026-08-29 (Fable)

## Goal

The platform survives a carrier dying mid-run and an operator restoring from backup.
Reality check from recon: P9b already shipped what PHASES.md lists first — Telnyx
messaging AND voice adapters with Ed25519 verification, the carrier registry, the
health circuit breaker (threshold 5 / cooldown 30 s), and `plan_route` with ordered
fallbacks. P14's real work is: making failover actually EXECUTE end-to-end (including
the credentials-death case the gate names), backups + a restore drill, a load test, a
runbook, number-reputation monitoring, a status surface, and the Tier-1 security
review.

## Design rulings (settled — do not relitigate in implementation)

- **DR-1: Dead credentials must open the breaker.** Today `auth` failures are excluded
  from `CARRIER_FAULT_CATEGORIES` ("our bugs don't trip failover"). That is right for
  `invalid_request` and stays; it is WRONG for auth: a revoked/rotated credential is
  operationally a dead carrier — exactly the gate's scenario. Ruling: `auth` joins the
  breaker-opening categories, with the SAME threshold (5 consecutive). A typo'd
  credential at setup never gets 5 consecutive real sends without the operator noticing
  the probe endpoint failing first.
- **DR-2: Failover executes IN the send path, same request.** `send_message` (via the
  routing seam) walks `RoutePlan.primary` then `fallbacks` on a carrier-fault
  SendResult, recording each attempt's failure on the breaker, honoring D12 exactly:
  intra-carrier by default, cross-carrier only when the org's policy allows it AND the
  send is not a mid-thread reply. The message row is created ONCE; only the
  (carrier, from) pair of the successful attempt lands on it — "no message lost" means
  the caller sees one message with one outcome, never two sends. Voice: `create_call`
  walks the same plan for the initial leg only (an established call never re-dials).
- **DR-3: Recovery is the breaker's existing half-open probe** — after cooldown one
  real send goes back to the primary; success closes the breaker ("and back after
  cooldown"). No new machinery; the gate test drives time via injected clocks (the
  breaker gets an injectable clock — it currently reads real time; make `now`
  injectable, default unchanged).
- **DR-4: Backups are boring and testable.** `scripts/backup.sh` (runs ON the box):
  `docker exec csaas-db-1 pg_dump -Fc` → `/opt/csaas/backups/csaas-YYYYmmdd-HHMM.dump`,
  keep 14, `chmod 600`. Installed as a root cron line (03:30 CT daily) by the operator
  per the runbook — deploy.sh does NOT install cron (it promises to touch nothing
  outside /opt/csaas). `scripts/restore_drill.sh` (also on-box): spin a THROWAWAY
  postgres container on an ephemeral port, `pg_restore` the newest dump into it, run
  `alembic upgrade head` against it (must be a no-op), run `scripts/smoke_restore.py`
  (row-count + integrity assertions per critical table + a tenant-isolation spot
  check), destroy the container. The drill NEVER touches the live DB or live compose
  project (distinct container name + no compose).
- **DR-5: The gate's "P1–P13 smoke tests" = the Postgres test suite against the
  restored schema shape**, not live-carrier calls (B1/B2 still absent). Concretely:
  restore drill proves schema+data integrity; the CI `test-postgres` job is the
  standing proof the suite passes on Postgres; `smoke_restore.py` proves the RESTORED
  database serves the app (boot the api image against the throwaway DB, `/healthz` db
  ok, one authenticated read per major surface using data that survived the dump).
  Recorded honestly in PROGRESS as the local equivalent of the gate sentence.
- **DR-6: Load test with zero new products.** `scripts/load_test.py` — asyncio + httpx
  (already deps): N concurrent senders against `/api/v1/messages` on the LOOPBACK
  carrier + the inbox read path, reporting p50/p95/p99 latency and error rate; runs
  locally and in a bounded mode against the VPS (`--rps` cap, loopback org only —
  NEVER against live carriers). Pass bar documented in the runbook (p95 < 250 ms at
  20 rps sustained on the VPS, error rate 0).
- **DR-7: Number reputation is DERIVED monitoring, v1.** No third-party reputation
  API. Per number over trailing 7 days: delivery rate, carrier-error rate, 4xxx
  spam-class error count (Bandwidth 4750/4770-class + Telnyx equivalents from the
  existing error classifiers), volume. `GET /api/v1/numbers/reputation` +
  a sweeper check that writes an `audit_log` row (`number.reputation_alert`) when a
  number crosses thresholds (delivery < 85% over ≥ 50 sends, or any spam-class error).
  Alerting beyond the audit row is out of scope.
- **DR-8: Status page.** Public `GET /status`: overall + per-component
  (`api`, `db`, `redis`, per-carrier breaker state, `media_plane` = LiveKit reachable)
  — component names and up/degraded/down ONLY, no versions, no counts, no carrier
  account detail. Served by the api (no new hosting), linked in the runbook. The
  operator-facing detail stays where it is (`/api/v1/routing/carriers`, authed).
- **DR-9: Runbook.** `docs/RUNBOOK.md`: deploy, rollback (git revert + redeploy;
  migrations are additive by policy), backup/restore drill, carrier failover manual
  override (pin/unpin via routing policy), breaker interpretation, log locations,
  the B1–B4 unblock steps, load-test invocation, incident quick-checks. Pull the
  media-plane bring-up section from PROGRESS so ops knowledge lives in ONE place.
- **DR-10: Security review is Tier-1 and produces findings, not vibes.**
  Fable reviews: authn/z surfaces (JWT, API keys, worker JWT, webhook verify paths,
  WS auth), tenant isolation hooks, secret handling (env, Fernet fields, logs),
  SSRF/egress (webhook deliverer, media fetch, carrier hosts), rate/abuse surfaces
  (public register, upload caps), and the deploy path. Findings land in
  `docs/SECURITY_REVIEW_P14.md` with severity; fixes that are small land in-phase,
  the rest go to OPEN_ISSUES.

## Schema

None. (Reputation derives from existing tables; breaker stays in-process by P3b DR-3.)

## Allowed files (implementer may read anything; WRITE only these)

- `backend/app/providers/health.py` (ONLY: add `auth` to fault categories + injectable
  clock per DR-1/DR-3)
- `backend/app/routing/router.py` + the send/create_call seam files the recon names as
  the routing entry points (`backend/app/services/messaging.py` is Fable-owned: if the
  failover walk needs an edit THERE, STOP and hand the exact diff to the orchestrator
  instead of making it)
- `backend/app/services/reputation.py` (new), `backend/app/services/sweeper.py` (ONLY
  the reputation check tick)
- `backend/app/api/routes/status.py` (new, public `/status`) + include line in
  `backend/app/main.py`; `backend/app/api/routes/numbers.py` (ONLY the reputation
  read endpoint)
- `backend/scripts/backup.sh`, `backend/scripts/restore_drill.sh`,
  `backend/scripts/smoke_restore.py`, `backend/scripts/load_test.py`
- `docs/RUNBOOK.md`
- `backend/tests/test_failover.py`, `test_reputation.py`, `test_status.py` (new)

## Forbidden (all implementers)

- `backend/app/models/**`, `backend/migrations/**`, `backend/app/auth/**`,
  `backend/app/compliance/**`, `backend/app/providers/**` other than health.py,
  `backend/app/services/messaging.py` (see DR-2 stop rule), `voice_plane/**`,
  `agents/**`, `.env*`, `deploy/**` (scripts live under backend/scripts), CI config,
  `pyproject.toml`, existing tests

## Test spec

- [ ] breaker: 5 consecutive `auth` failures open it (NEW behavior); `invalid_request`
      still never does; half-open after cooldown via injected clock; success closes
- [ ] **THE GATE (local form):** two fake carriers registered (primary + fallback),
      org policy `allow_cross_carrier_failover=True`; primary scripted to throw
      auth errors mid-run → sends 1–5 fail over per-attempt to fallback and every
      message row lands `sent` exactly once on SOME carrier (none lost, none doubled);
      after cooldown (injected clock) the next send goes back to primary
- [ ] mid-thread reply with cross-carrier failover allowed → still REFUSES the carrier
      switch (D12), intra-carrier fallback only
- [ ] voice create_call walks the plan the same way (fake voice carriers)
- [ ] reputation: seeded DLRs → correct rates; threshold breach writes the audit row
      once (idempotent per day)
- [ ] status: `/status` unauthenticated, shape stable, no secret-ish fields, degraded
      when a breaker is open
- [ ] scripts: `load_test.py --self-test` runs a tiny loopback burst in-process;
      `smoke_restore.py` importable + unit-tested assertions

Manual (on the box, Fable-supervised, in-phase):
- [ ] run `backup.sh`; run `restore_drill.sh` end-to-end green
- [ ] `load_test.py` bounded run against the VPS loopback org; record numbers in the
      runbook

Pass criteria: full backend suite green + ruff; CI green after push; drill + load test
executed on the box with outputs recorded in RUNBOOK.md.

## Deploy

yes — code deploy + on-box drill; cron line installed manually per runbook.
