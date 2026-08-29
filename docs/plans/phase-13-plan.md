# Phase 13 — Analytics + platform services

**WS:** 9, 7 · **Deploy:** yes · **Planned:** 2026-08-29 (Fable)

## Goal

The platform grows its service layer: per-tenant usage metering with a carrier
reconciliation report, a public machine API behind scoped rotatable API keys, signed
outbound webhooks with retries and replay dedupe, an audit log, transcript search,
LLM sentiment/call-scoring (seamed, honest about B4), and an analytics dashboard.

## Design rulings (settled — do not relitigate in implementation)

- **DR-1: No OpenMeter, no Svix.** Both are external services; this VPS is shared with
  other businesses and D8's spirit (no heavy third-party cores) applies. We build the
  LEAN version in-house — a metering rollup and a signed deliverer — behind narrow
  seams (`services/usage.py`, `services/webhooks_out.py`) so either SaaS can replace
  the implementation later without touching callers. PHASES.md named them as
  suggestions, not adopted decisions; this ruling is the decision.
- **DR-2: Usage is DERIVED, rollups are idempotent.** `usage_records` daily rollup per
  (org, UTC date, metric): `sms_segments` (carrier-reported count when present, else
  estimate — the reconciliation report shows both sides and the delta), `mms_messages`,
  `voice_minutes` (ceil of Call.duration_seconds/60), `ai_sms_turns`, `ai_tokens`
  (from the new token columns), `storage_bytes` (media + recordings). The sweeper
  upserts on UNIQUE (org_id, period_date, metric) — recomputing a day REPLACES the
  quantity (derived, never incremented). `GET /api/v1/usage/reconciliation?date=`
  returns estimate vs carrier per metric with a tolerance verdict — THE GATE query.
- **DR-3: API keys.** Format `csk_<8-char-prefix>_<32-char-secret>`; DB stores prefix +
  SHA-256 hash only (constant-time compare; argon2 is for passwords, not
  high-frequency machine auth). Org-bound (no X-Org-Id needed), scoped to a SUBSET of
  the RBAC permission catalogue, `expires_at` optional, revocation immediate, rotation
  = create-new + revoke-old. Every use stamps `last_used_at` (best-effort). The
  Fable-written dependency `require_api_scope(...)` produces the same OrgContext shape
  routes already consume. A revoked/expired key is 401, a valid key without the scope
  is 403 — never merged.
- **DR-4: Outbound webhooks ride a DURABLE outbox, not the in-process bus.** The bus
  drops on overflow and dies with the process — fine for UI freshness, unacceptable
  for customer webhooks. `platform_events` (outbox) rows are written IN THE SAME
  TRANSACTION as the domain change by `record_platform_event(session, org_id, type,
  payload)`. Fable inserts the six v1 hooks (Tier-1, in owned files):
  `message.received`, `message.finalized` (delivered/failed DLR), `call.completed`,
  `voicemail.created`, `campaign.completed`, `appointment.booked`.
- **DR-5: Delivery + signing.** `webhook_endpoints` (url, secret shown once,
  subscribed event types, status) × outbox → `webhook_deliveries` (UNIQUE
  (endpoint_id, event_id)). Sweeper tick delivers pending rows: headers
  `X-Webhook-Id` (the stable event id — consumer dedupe key), `X-Webhook-Timestamp`,
  `X-Webhook-Signature: v1=HMAC_SHA256(secret, "{id}.{timestamp}.{body}")` —
  Svix-compatible scheme, so DR-1's swap stays cheap. Retry backoff
  [60s, 5m, 30m, 2h, 12h] then `dead`; 2xx = delivered; an endpoint with 20
  consecutive failures auto-disables (audit-logged). Replay of the same event to the
  same endpoint is impossible by constraint — a REDELIVERY (manual retry) reuses the
  same X-Webhook-Id so consumer-side dedupe still holds — THE GATE's second sentence.
  SSRF guard: https only (http allowed only for localhost in dev settings), no
  requests to private IP ranges in production; deliverer never follows redirects.
- **DR-6: Audit log.** `audit_log` append-only: actor (user id OR api key id), action
  (dotted verb, e.g. `apikey.created`, `webhook_endpoint.updated`, `campaign.started`,
  `flow.activated`, `role.changed`, `supervisor.barge`), target type/id, detail JSON
  (NEVER secrets), created_at. `services/audit.py::record(...)` writes in-transaction.
  v1 wires every NEW P13 mutating route + campaign start/pause/cancel + flow
  activate/bind + supervisor actions + role changes. Reads: `GET /api/v1/audit`
  (org:update permission), filterable, cursor-paginated.
- **DR-7: Transcript search is portable-first.** `GET /api/v1/search/transcripts?q=`
  uses LIKE on lower() (portable, tested on SQLite); on Postgres the SAME endpoint
  uses `websearch_to_tsquery` against a generated tsvector GIN index (created in the
  migration behind a dialect guard, exercised by a `pg_only` test). Results grouped by
  call with matching segments + timestamps.
- **DR-8: Sentiment + call scoring is a seam, honest about B4** (same shape as P12
  voicemail transcription). `call_scores` (call_id UNIQUE, sentiment
  `positive|neutral|negative`, score 1–5, summary, status
  `pending|done|failed|disabled`). Sweeper scores completed calls that have
  transcripts, via the EXISTING `services/llm_client.py` (P10) — no new LLM code path;
  no key → `disabled`, never a fake score. Tests use MockTransport.
- **DR-9: AI token accounting starts now.** `agent_sms_turns` gains nullable
  `tokens_in`/`tokens_out`; the SMS agent records usage from the LLM response
  (Fable makes this edit — sms_agent stays otherwise closed). Voice-agent tokens live
  in the worker and are OPEN_ISSUES (I6) until the worker seam reports them.
- **DR-10: Dashboards derive, never count.** `GET /api/v1/analytics/overview?days=N`:
  daily series (messages in/out, delivery rate from carrier-terminal statuses, calls +
  avg duration, campaign progress, AI turns + handoffs), one aggregate SQL query per
  series, org-scoped. Frontend `DashboardPage` — **recharts approved** (new frontend
  dep, Fable Tier-1 ruling; it is the standard lean React chart lib).
- **DR-11: The public API is the SAME API.** API keys authenticate against the
  existing `/api/v1` routes through scopes — no separate "public API" router, no
  drift. Docs: the OpenAPI schema already exists; `GET /api/v1/openapi-public.json`
  filters to key-reachable routes. Machine callers never get JWT-only endpoints
  (auth/2FA/org-switching are scope-less by construction).

## Schema (Tier-1, done by Fable — implementers do not touch)

Migration `0014_platform_services` (additive):
- `api_keys`: id, org_id, name, prefix (UNIQUE), key_hash (sha256 hex), scopes
  (PortableJSON list), status (`active|revoked`), expires_at (nullable),
  last_used_at (nullable), created_by, timestamps.
- `platform_events`: id, org_id, event_type, payload (PortableJSON), created_at.
  Index (org_id, created_at).
- `webhook_endpoints`: id, org_id, url, secret_hash… (secret stored ENCRYPTED with
  credential_encryption_key — deliverer needs it back), event_types (PortableJSON),
  status (`active|disabled`), failure_streak (int), created_by, timestamps.
- `webhook_deliveries`: id, org_id, endpoint_id (FK CASCADE), event_id (FK
  platform_events CASCADE), event_type, status (`pending|delivered|failed|dead`),
  attempts, next_attempt_at, last_status_code (nullable), last_error (nullable),
  timestamps. UNIQUE (endpoint_id, event_id); index (status, next_attempt_at).
- `audit_log`: id, org_id, actor_user_id (nullable), actor_api_key_id (nullable),
  action, target_type, target_id (nullable str), detail (PortableJSON), created_at.
  Index (org_id, created_at).
- `call_scores`: id, org_id, call_id (FK CASCADE, UNIQUE), sentiment (nullable),
  score (nullable int), summary (nullable), status (default `pending`), timestamps.
- `usage_records`: id, org_id, period_date (Date), metric, quantity (BigInteger),
  carrier_quantity (nullable BigInteger), timestamps.
  UNIQUE (org_id, period_date, metric).
- `agent_sms_turns` + `tokens_in`, `tokens_out` (nullable Integer).
- Postgres-only: GIN tsvector index on call_transcripts.text (dialect-guarded).

Fable also lands: `require_api_scope` auth dependency, the six outbox hooks (DR-4),
and the sms_agent token capture (DR-9) — before implementer handoff.

## Allowed files (implementer may read anything; WRITE only these)

Backend:
- `backend/app/services/apikeys.py`, `webhooks_out.py`, `audit.py`, `usage.py`,
  `scoring.py`, `analytics.py`, `search.py` (all new)
- `backend/app/services/sweeper.py` (ONLY add: webhook delivery, usage rollup,
  call scoring ticks)
- `backend/app/api/routes/platform.py` (new: api-keys, webhook endpoints +
  deliveries + manual redeliver, audit read, usage + reconciliation),
  `backend/app/api/routes/analytics.py` (new: overview, transcript search) + two
  include lines in `backend/app/main.py`
- audit hooks in: `backend/app/api/routes/outbound.py` (campaign start/pause/cancel),
  `backend/app/api/routes/flows.py` (activate/bind, supervisor actions),
  `backend/app/api/routes/team.py`-equivalent (role changes) — audit calls ONLY,
  no logic changes
- `backend/tests/test_apikeys.py`, `test_webhooks_out.py`, `test_audit.py`,
  `test_usage.py`, `test_scoring.py`, `test_analytics.py` (new)

Frontend:
- `frontend/package.json` (ONLY: add recharts — pre-approved),
  `frontend/src/pages/DashboardPage.tsx`, `frontend/src/pages/PlatformPage.tsx`
  (API keys + webhooks + audit), nav/router wiring, regenerated hooks/types,
  matching tests

## Forbidden (all implementers)

- `backend/app/models/**`, `backend/migrations/**`, `backend/app/auth/**`
- `backend/app/services/messaging.py`, `sms_agent.py`, `llm_client.py` (call it,
  never edit), `compliance/**`, `providers/**`, `routing/**`, `voice_plane/**`
- `agents/**`, `.env*`, `deploy/**`, CI config, `backend/pyproject.toml`

## Test spec

- [ ] api keys: create returns full key ONCE; auth works against a real route with the
      right scope; missing scope 403; revoked/expired 401; rotation flow; last_used_at
      stamps; hash-only storage (no plaintext in DB)
- [ ] outbox: each of the six hooks writes a platform_event in the same transaction
      (spy at commit); no event on rollback
- [ ] webhooks: endpoint subscribed to type gets a delivery row per event (UNIQUE
      dedupes); tick delivers with correct signature (recompute HMAC in test against a
      fake server); 500 → retry with backoff schedule; exhausted → dead; 20-streak →
      endpoint auto-disabled + audit row; manual redeliver reuses the SAME
      X-Webhook-Id; non-subscribed type → no delivery
- [ ] usage: rollup derives correct quantities from seeded messages/calls/turns;
      re-running a day replaces, never doubles; reconciliation endpoint reports
      est vs carrier delta and verdict within tolerance (THE GATE, carrier side seeded
      via fixture DLR segment counts)
- [ ] audit: each wired action writes actor/action/target; api-key actor recorded;
      read endpoint filters + paginates
- [ ] scoring: completed call with transcript + mocked LLM → sentiment/score/summary
      `done`; no key → `disabled`; LLM error → `failed` and retried once
- [ ] search: LIKE path finds transcript text on SQLite (portable); `pg_only` test
      exercises the tsvector path
- [ ] analytics overview: series shapes correct against seeded data
- [ ] frontend: dashboard renders series; platform page creates a key (shows once) and
      an endpoint

Manual (live): none required beyond CI — the gate is fully local. LLM scoring live run
blocked on B4 (recorded).

Pass criteria: full backend suite green + ruff + frontend + OpenAPI drift gate; CI
(incl. Postgres) green after push.

## Deploy

yes — migrate + restart api.
