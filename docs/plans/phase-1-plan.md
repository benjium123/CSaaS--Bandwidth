# Phase 1 — Carrier layer + first SMS round-trip

> Refined by Fable (Tier-1) 2026-08-26. Workstreams: WS-1 (CAL), WS-2 (Messaging).
> Builds on P0 (31 tests green, session-layer tenancy, settings validation).
> **R1 is still open — we do not have a confirmed Bandwidth account.** This plan is
> therefore split into **P1a** (buildable and provably tested against recorded/constructed
> fixtures today, on Python 3.10 + SQLite) and **P1b** (the live round-trip, blocked on R1).
> Each has its own gate. P1a is not a stub: it ships the complete carrier layer, webhook
> ingestion, schema, state machine and send API, all exercised end-to-end by a fixture-driven
> wire test that differs from production only in who is posting the webhooks.

## Goal

Ship the Carrier Abstraction Layer (D2: event in → async command out), the Bandwidth
messaging adapter, webhook ingestion that survives Bandwidth's real behaviour (Basic auth,
24 h unordered parallel retries, JSON-array payloads), the message/thread/event schema with
an idempotency ledger, a monotonic message state machine, a tenanted send/read API, and the
compliance seam every send passes through. Everything tenant-scoped rides the P0
`TenantScoped` mixin and inherits the session-layer isolation guarantee.

**P1a gate (today, no carrier):** the full suite green on Python 3.10 + SQLite and on the
Postgres CI job, including: a fixture round-trip test (API send → mocked 202 → webhook
`delivered` posted *before* `sending`, each replayed 3×) ending in exactly the same DB state
as the ordered single-delivery run; a compliance-seam deny test proving zero carrier calls;
and tenancy tests proving org B sees none of org A's messages, threads or numbers.

**P1b gate (blocked on R1):** send an SMS from the API to a real handset; reply from the
handset and see it land as an inbound message in the right thread; DLR recorded with the
carrier-reported segment count; the recorded live webhook payloads committed as fixtures and
diffed against our constructed ones; the `live_carrier` suite green against the deployed
instance.

---

## Decision Record (settled for P1 — do not relitigate in implementation)

### DR-1: Split around R1 → **P1a (fixtures) / P1b (live)**

The stated P1 gate needs a real carrier account we do not have. Waiting idles the whole
dependency graph (P2, P5 both hang off P1). So: **P1a is the entire codebase of the phase**
— adapter, ingestion, schema, state machine, API, seam — proven by construction against the
exact payload shapes documented in `docs/research/bandwidth.md`. **P1b adds zero production
code**: it is credentials, the Bandwidth dashboard callback configuration, nginx exposure of
the webhook path, a smoke script run, and fixture verification. If live payloads differ from
our constructed fixtures, that is a P1b *finding* fixed under Opus review — the architecture
does not change. PHASES.md's P1 gate = P1b gate; P2 may start on P1a sign-off because P2
consumes the models and API, not the live carrier.

### DR-2: CAL interface → **`MessagingCarrier` protocol, frozen-dataclass domain objects, capabilities declared not discovered**

Per D2 the internal model is Telnyx-shaped. For **messaging** specifically, Bandwidth is
*not* document-return — messaging callbacks expect a bare 2xx and ignore the response body
(document-return is a **voice** constraint; P5 inherits it, and the `providers/` package
layout reserves room for it). The messaging interface is therefore cleanly async:

```python
class MessagingCarrier(Protocol):
    name: str                                  # "bandwidth" | "telnyx" (P14)
    capabilities: CarrierCapabilities
    async def send_message(self, msg: OutboundMessage) -> SendResult: ...
    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool: ...
    def parse_webhook(self, raw_body: bytes) -> list[CarrierEvent]: ...
```

Domain objects (`providers/domain.py`, all `@dataclass(frozen=True)`, carrier-neutral,
zero SQLAlchemy imports):

- `OutboundMessage(to: str, from_: str, text: str, media: tuple[str, ...], tag: str)`
  — `tag` is OUR correlation id (`str(message.id)`), echoed back by the carrier.
- `SendResult(status: Literal["accepted", "rejected"], provider_message_id: str | None,
  error: CarrierError | None)` — **there is deliberately no "delivered" member.** Bandwidth
  returns 202; delivery is only ever learned via webhook. The type makes it impossible for
  a caller to believe otherwise.
- `InboundMessage(provider_message_id, from_, to, our_number, text, media, segment_count,
  event_time, raw)` and `DeliveryReceipt(provider_message_id, event_type, error_code,
  error_description, segment_count, event_time, raw)`;
  `CarrierEvent = InboundMessage | DeliveryReceipt`. `raw` carries the full original event
  dict for the ledger.
- `CarrierError(category: Literal["invalid_request", "unregistered", "rate_limited",
  "carrier_transient", "carrier_unreachable", "auth"], carrier_code: str | None,
  retryable: bool, detail: str)`.
- `CarrierCapabilities(supports_cancel=False, supports_scheduled_send=False,
  sync_delivery_status=False, max_media_bytes=3_750_000, group_mms_toll_free=False)`.

**What the Bandwidth adapter CANNOT express, and how callers learn it:**
1. *Synchronous delivery status* — encoded in the `SendResult` type (no delivered state)
   and `capabilities.sync_delivery_status=False`.
2. *Cancelling a queued message / scheduled send* — the protocol has no such methods in P1;
   when P11 wants them it consults `capabilities` first, and an adapter asked for an
   unsupported op raises `CarrierCapabilityError` (new, 501, code
   `carrier_capability_unsupported`).
3. *Per-message rate feedback* — Bandwidth rate-limits by **segment** per number/campaign
   and answers 429 at the account edge; the adapter maps that to
   `CarrierError(category="rate_limited", retryable=True)`. Pacing is P11's job; P1 records
   the rejection, it does not retry (DR-7).

The adapter is selected once at app startup: lifespan builds the carrier from settings and
stores it at `app.state.carrier` (or `None` when Bandwidth is disabled — the R1 reality).
A `get_carrier` dependency returns it or raises `CarrierNotConfiguredError` (503,
`carrier_not_configured`). Tests inject a `FakeCarrier` by setting `app.state.carrier`.
**Nothing outside `app/providers/` imports httpx or knows a Bandwidth URL** — that is the
WS-1 invariant, and the Ruff-checkable proxy for it is: no module outside `app/providers/`
may import `app.providers.bandwidth`.

### DR-3: No Bandwidth SDK; direct REST via injected `httpx.AsyncClient`

The messaging surface P1 needs is exactly one endpoint
(`POST https://messaging.bandwidth.com/api/v2/users/{accountId}/messages`). The unified
`bandwidth-sdk` drags a large, wildly version-skewed dependency (research: Python 23.x vs
Node 6.x) for one call, and would sit between us and `httpx.MockTransport`. The adapter's
constructor takes an optional `httpx.AsyncClient`; production builds one with Basic auth
and a 10 s timeout; tests pass `AsyncClient(transport=MockTransport(handler))`. New runtime
dependencies this phase: **`phonenumbers`** only (already sanctioned by D3), plus nothing
else without Fable approval. Revisit the SDK when P4 (numbers/10DLC) multiplies the surface.

### DR-4: Webhook ingestion → **auth 401 / persist-first idempotency / per-event outcome taxonomy: DONE-200, RETRY-500, DEAD-LETTER-200**

- **Auth:** HTTP Basic against `settings.bandwidth_webhook_username/_password` (both exist
  in P0 config). Verification uses `hmac.compare_digest` on username **and** password,
  combined with `&` — both comparisons always run; no short-circuit, no timing oracle.
  Failure → 401, nothing persisted. (Bandwidth retrying a 401 for 24 h is their problem;
  accepting unauthenticated events would be ours.)
- **Payload is a JSON ARRAY.** Each event in the array is processed **independently, each
  in its own committed transaction** — one bad event must not poison a batch.
- **Idempotency is a DB constraint, not an application check.** Bandwidth publishes no
  event id, so the dedupe key is `(carrier, provider_message_id, event_type)` — unique
  constraint on `message_events`. Ingestion INSERTs the event row *first*; an
  `IntegrityError` means this exact event was already ingested (possibly by a parallel
  in-flight retry — Bandwidth retries in parallel) → roll back, count as DONE, no side
  effects. This survives true concurrency on Postgres; the `pg_only` job proves it.
- **Per-event outcomes:**
  - `DONE` — persisted + processed, or a dedupe hit.
  - `RETRY` — a DLR whose `provider_message_id` matches no message row. This is the
    send-race: the webhook can arrive between our carrier call and our commit. We answer
    **500 deliberately** so Bandwidth's own 24 h retry re-delivers after our transaction has
    landed. A genuinely alien id stops costing anything after 24 h. Nothing is persisted
    for a RETRY event.
  - `DEAD_LETTER` — malformed JSON, unknown event type, or an inbound to a number no org
    owns. Retrying cannot fix these, so: write a `webhook_dead_letters` row (raw body,
    reason; **never** the Authorization header) and answer 200 so the carrier stops.
  - **Batch response:** 500 if *any* event returned RETRY, else 200. Safe because every
    already-DONE event in the replayed batch dedupes to a no-op.
- **2xx-fast:** ingestion does DB work only — no external I/O of any kind in the webhook
  path (design rule, enforced in review). That keeps ack far under the 2 s budget without
  needing a queue. The seam to a real worker is already cut: events carry
  `processed_at`/`processing_error`, processing is the idempotent, ledger-driven
  `process_event(session, event_row)`, and `reprocess_pending(session)` re-drives any event
  whose `processed_at` is NULL. Scheduling `reprocess_pending` behind Redis is P3+; its
  existence and its test are P1.
- **Tenancy in the webhook path:** the request has no user and no `X-Org-Id`. Exactly two
  `allow_unscoped` lookups are permitted, each with a justifying comment: (a) resolving an
  inbound event's `message.owner` number → `org_numbers` row; (b) resolving a DLR's
  `provider_message_id` → `messages` row. The instant an org is resolved,
  `set_org_context(session, org_id)` is called and everything after runs scoped. The
  session-layer guard remains armed the whole time.

### DR-5: Message state machine → **monotonic ranks; terminal states immutable; events ledgered even when they don't transition**

Statuses (stored as short strings, validated in code — no `sa.Enum` DDL, it is a migration
tax on both dialects): outbound `queued → accepted → sending → delivered | failed`, plus
`rejected` (carrier refused the send API call or was unreachable); inbound is always
`received`. Ranks: `queued=0, accepted=10, sending=20, delivered=30, failed=30,
rejected=30`. Terminal set: `{delivered, failed, rejected}`.

Transition rule (pure function `apply_event(current, event_type) -> new_status | None` in
`services/messaging.py`, unit-tested exhaustively):
- allowed iff `current` is not terminal **and** `rank(new) > rank(current)`;
- **`delivered` arriving before `sending`** (this WILL happen — retries are unordered):
  `delivered` applies (30 > 10); the late `sending` then fails the rank test → status
  untouched, but the event row is already in the ledger — the audit trail is complete even
  though the status never regressed;
- conflicting terminals (`failed` after `delivered` or vice versa): first terminal wins,
  the loser is ledgered and logged at WARNING with both codes — never overwritten;
- `message-failed` carries top-level `errorCode`: recorded into `messages.error_code` +
  `error_detail` on transition;
- any event carrying `segmentCount` reconciles `messages.segment_count_carrier`
  (carrier value is truth; a mismatch with our estimate logs at DEBUG, nothing more).

### DR-6: Schema → **five tables; the thread primitive is a number-pair bucket, not P2's inbox**

`messages`, `message_events` (idempotency + audit ledger), `message_threads`, `org_numbers`
(minimal — P4 owns real inventory and *extends* this table), `webhook_dead_letters`
(deliberately NOT tenant-scoped — a dead letter exists precisely because no org could be
resolved). Threading decision: P1 stamps every message with a `thread_id` at write time,
where a thread is nothing but the unique bucket `(org_id, our_e164, contact_e164)` with a
`last_message_at`. No participants, no assignment, no read-state, no contact linkage — all
P2. Rationale: threading retrofitted over raw messages is a backfill migration; threading
at write time is one upsert. **Segment counting is in P1** (estimate at send, carrier
value from DLRs) because Bandwidth bills and rate-limits by segment: P11's pacing and P13's
metering both need the column populated from the first message ever sent, and the
estimator is ~40 pure lines that are trivially unit-testable now.

### DR-7: Send path → **carrier rejection is data, not an HTTP error; no auto-retry in P1**

`POST /api/v1/messages` returns **201 with the message resource whenever a row was
created** — including `status="rejected"` with `error_code` populated when the carrier
refused or was unreachable. HTTP errors are reserved for request-level problems: 422
validation / unknown from-number / `compliance_blocked`, 503 `carrier_not_configured`.
Rationale: even "accepted" is not delivery — the whole channel is async, and a client that
must handle DLR-driven failure anyway should read one uniform resource. P1 performs **no
send retries** (a transient failure → `rejected` + `error_code=carrier_unreachable`,
retryable-ness visible in the error taxonomy): retry policy belongs to P11's pacing engine,
and a naive auto-retry here would double-send the moment it met a real 429.

### DR-8: Compliance seam → **one choke point, called before any row or carrier touch; P1 verdict is always allow**

`app/compliance/gate.py` exposes `async check_outbound(session, org_id, draft) ->
ComplianceVerdict(allowed: bool, reason: str | None)` and `async on_inbound(session,
org_id, inbound) -> None`. P1 implementations: allow-all / no-op. The send service calls
`check_outbound` **exactly once, before the message row is created and before the carrier
is touched**; a deny raises `ComplianceBlockedError` (422, `compliance_blocked`) with no
row persisted — P3 owns the audit ledger of blocks. The inbound path calls `on_inbound`
after persisting each inbound message (P3 hangs STOP/HELP/START off it). Tests pin the
seam with a spy so P3 fills it in rather than retrofitting it. **No compliance logic in
P1** — no keyword parsing, no quiet hours, nothing.

---

## Pre-dependencies

- P0 merged; suite green (`31 passed, 1 skipped`).
- Add to `backend/pyproject.toml` runtime deps: **`phonenumbers>=8.13`**. No other new
  dependency without Fable approval (explicitly NOT `bandwidth-sdk` — DR-3).
- Register the **`live_carrier`** pytest marker in `pyproject.toml` (strict markers are on;
  an unregistered marker fails collection).
- `.env` already carries every Bandwidth field the code reads (`BANDWIDTH_ENABLED`,
  `BANDWIDTH_ACCOUNT_ID`, `_API_USERNAME`, `_API_PASSWORD`,
  `_MESSAGING_APPLICATION_ID`, `_WEBHOOK_USERNAME`, `_WEBHOOK_PASSWORD`,
  `_DEFAULT_NUMBER`) — settings changes are NOT needed; do not add fields.
- Live-test-only variables (`BANDWIDTH_LIVE_TEST`, `SMOKE_BASE_URL`,
  `BANDWIDTH_TEST_RECIPIENT`) are read from `os.environ` inside the live tests/script
  only — they never enter `Settings` and `.env.example` is not touched.
- **P1b only (user-owned, not implementer work):** R1 resolved with production
  credentials; Bandwidth messaging application's callback URL pointed at
  `https://<public-host>/api/v1/webhooks/bandwidth/messaging` with the Basic-auth
  credentials set in the dashboard; nginx TLS exposure of that one path on the VPS
  (operator-supervised — the box runs live production for other businesses).

## Allowed Files (implementer may create/read/write — nothing else)

```
backend/pyproject.toml                                  (add phonenumbers; add live_carrier marker)
backend/app/providers/__init__.py
backend/app/providers/domain.py
backend/app/providers/base.py
backend/app/providers/segments.py
backend/app/providers/bandwidth/__init__.py
backend/app/providers/bandwidth/adapter.py
backend/app/providers/bandwidth/webhooks.py
backend/app/providers/bandwidth/errors.py
backend/app/models/messaging.py
backend/app/models/__init__.py                          (export new models)
backend/app/services/__init__.py
backend/app/services/messaging.py
backend/app/compliance/__init__.py
backend/app/compliance/gate.py
backend/app/api/routes/messages.py
backend/app/api/routes/numbers.py
backend/app/api/routes/webhooks.py
backend/app/main.py                                     (mount routers; carrier in lifespan — surgical edit)
backend/app/errors.py                                   (add the three P1 error classes only)
backend/migrations/versions/0002_messaging.py
backend/tests/conftest.py                               (add fixtures/helpers only; do not alter P0 fixtures)
backend/tests/fixtures/bandwidth/message-received.json
backend/tests/fixtures/bandwidth/message-sending.json
backend/tests/fixtures/bandwidth/message-delivered.json
backend/tests/fixtures/bandwidth/message-failed.json
backend/tests/fixtures/bandwidth/batch-two-events.json
backend/tests/fixtures/bandwidth/send-202.json
backend/tests/fixtures/bandwidth/send-400-4720.json
backend/tests/fixtures/bandwidth/send-429.json
backend/tests/test_segments.py
backend/tests/test_bandwidth_adapter.py
backend/tests/test_webhook_ingest.py
backend/tests/test_message_state_machine.py
backend/tests/test_send_api.py
backend/tests/test_compliance_seam.py
backend/tests/live/__init__.py
backend/tests/live/test_live_roundtrip.py
backend/scripts/smoke_sms.py
deploy/nginx-csaas.conf                                 (reference config only; installing it is P1b, operator-run)
docs/PROGRESS.md                                        (status updates only)
```

## Forbidden (implementer must never touch)

- `.env` / `.env.example` / any secrets, local or VPS.
- `docs/` other than `docs/PROGRESS.md`.
- **All P0 modules not listed above** — in particular `app/db/base.py`, `app/db/types.py`,
  `app/db/session.py`, `app/config.py`, `app/auth/*`, `app/repositories/*`,
  `migrations/versions/0001_foundation.py`, the P0 test files, `.github/workflows/ci.yml`,
  `deploy/Dockerfile`, `deploy/docker-compose.prod.yml`, `deploy/deploy.sh`. If P1 seems
  to need a change there, STOP and report — do not make it.
- Anything on the VPS. P1a deploys nothing; P1b's deploy is operator-supervised.
- No new dependencies beyond `phonenumbers`. No Redis usage, no queue library, no
  Bandwidth/Telnyx SDK, no Telnyx code of any kind (P14 — the *interface* accommodates it;
  the adapter does not exist yet).
- `main.py` and `errors.py` edits are surgical: routers + lifespan carrier wiring, and the
  three new error classes. Nothing else in those files moves.

## Implementation Notes

### 1. `providers/domain.py` + `providers/base.py`

Exactly the shapes in DR-2. `base.py` also holds
`build_carrier(settings) -> MessagingCarrier | None` (returns the Bandwidth adapter iff
the bandwidth provider status is enabled, else `None`) and the `get_carrier` FastAPI
dependency (reads `request.app.state.carrier`, raises `CarrierNotConfiguredError` on
`None`). New error classes in `app/errors.py`:
`CarrierNotConfiguredError` (503, `carrier_not_configured`),
`ComplianceBlockedError` (422, `compliance_blocked`),
`CarrierCapabilityError` (501, `carrier_capability_unsupported`).

### 2. `providers/segments.py`

Pure function `estimate(text: str) -> SegmentEstimate(encoding: Literal["gsm7","ucs2"],
segments: int, septets_or_chars: int)`. GSM-7 basic + extension sets as module frozensets;
extension chars (`€ [ ] { } | ^ ~ \\`) cost 2 septets. Boundaries: GSM-7 → 160 single /
153 per part; UCS-2 → 70 single / 67 per part. One non-GSM char flips the whole message to
UCS-2 (per spec). Empty text → 1 segment. No dependencies.

### 3. `providers/bandwidth/adapter.py` / `webhooks.py` / `errors.py`

- `BandwidthMessagingCarrier(settings-ish config, client: httpx.AsyncClient | None)`.
  `send_message`: `POST /api/v2/users/{account_id}/messages` with
  `{"to": [msg.to], "from": msg.from_, "text": msg.text, "applicationId": ...,
  "tag": msg.tag}` (+ `media` when present). 202 → `SendResult("accepted",
  id_from_body, None)`. Non-202 → map via `errors.py`:
  400-class body code `4302/4403/4411/4720` → `invalid_request`; `4476` → `unregistered`
  (log at ERROR — this is the Track-R tripwire); 429 or `4780` → `rate_limited`
  (retryable); 5xx / `56xx` → `carrier_transient` (retryable); `httpx.TransportError` →
  `carrier_unreachable` (retryable); 401/403 → `auth`. Mapping is a pure function
  `classify(status_code, body) -> CarrierError`, unit-tested per code.
- `webhooks.py` is pure (no DB, no I/O): `verify(headers, expected_user, expected_pass)`
  per DR-4's constant-time rule; `parse(raw_body) -> list[CarrierEvent]` raising
  `ValueError` on non-array/malformed input, mapping `message-received` →
  `InboundMessage` (our number = `message.owner`; contact = `message.from`) and
  `message-sending|delivered|failed` → `DeliveryReceipt` (with top-level `errorCode`/
  `description` on failed). Unknown `type` → a sentinel `UnknownEvent(raw)` the service
  dead-letters. Timestamps parsed to tz-aware datetimes.

### 4. Schema (`models/messaging.py`, migration `0002_messaging`)

All tables `GUID` PKs (app-side uuid4) + `TimestampMixin`; tenant tables use
`TenantScoped` — that is the isolation contract.

- **org_numbers** (TenantScoped): `e164` String(20) **globally unique** (a number belongs
  to exactly one org), `carrier` String(16) default `"bandwidth"`, `is_active` bool.
  P4 extends this table (adds columns); it does not replace it.
- **message_threads** (TenantScoped): `our_e164`, `contact_e164` String(20),
  `last_message_at` DateTime(tz), unique `(org_id, our_e164, contact_e164)`
  (`uq_threads_org_pair`). Upsert = insert, catch `IntegrityError`, re-select — race-safe
  on both dialects.
- **messages** (TenantScoped): `thread_id` FK→message_threads `RESTRICT` NOT NULL,
  `direction` String(8) (`outbound|inbound`), `status` String(16) (DR-5 set, code-level
  validation), `from_e164`, `to_e164` String(20), `body` Text nullable, `media`
  PortableJSON default list (inbound MMS URLs can arrive before P3 — store, don't fetch),
  `carrier` String(16), `provider_message_id` String(64) nullable + **unique index
  `(carrier, provider_message_id)`** (NULLs distinct on both dialects — fine),
  `segment_count_est` Integer nullable, `segment_count_carrier` Integer nullable,
  `error_code` String(32) nullable, `error_detail` String(255) nullable. The carrier `tag`
  is `str(id)` by construction — no extra column. Index on `(org_id, thread_id,
  created_at)` for the read API.
- **message_events** (TenantScoped): `message_id` FK→messages CASCADE NOT NULL, `carrier`,
  `provider_message_id` String(64), `event_type` String(32), `payload` PortableJSON (full
  raw event), `event_time` DateTime(tz) nullable, `processed_at` DateTime(tz) nullable,
  `processing_error` String(255) nullable. **Unique `(carrier, provider_message_id,
  event_type)` — `uq_msg_events_dedupe`. This constraint IS the idempotency mechanism.**
- **webhook_dead_letters** (NOT TenantScoped — by definition no org resolved): `carrier`
  String(16), `reason` String(64), `payload` Text (raw body — it may not even be JSON).
  Never stores auth headers. No API surface in P1; ops table.

### 5. Ingestion (`api/routes/webhooks.py` + `services/messaging.py`)

`POST /api/v1/webhooks/bandwidth/messaging` — no JWT, no `X-Org-Id`; carrier Basic auth is
the only gate. Flow per DR-4:

```
verify(headers) or 401
parse raw body; ValueError → dead_letter("malformed") → 200
outcomes = [await ingest_one(e) for e in events]     # each in its own transaction
return 500 if RETRY in outcomes else 200
```

`ingest_one(event)`:
- `InboundMessage`: resolve `org_numbers.e164 == our_number` **(allow_unscoped, justified:
  pre-tenant-resolution lookup)**; miss → dead_letter(`"unknown_number"`) → DONE.
  `set_org_context`; INSERT event row (message row must exist first — create the inbound
  `Message(status="received", ...)` + thread upsert, then the event row; on the event
  row's `IntegrityError` roll back everything from this event → DONE dedupe). Order note:
  to keep the FK satisfied and the dedupe atomic, do it in ONE transaction: upsert thread →
  insert message (catch dup via the `(carrier, provider_message_id)` unique on messages —
  a duplicate inbound dedupes there) → insert event → `on_inbound(...)` → commit.
- `DeliveryReceipt`: look up message by `(carrier, provider_message_id)`
  **(allow_unscoped, justified: DLRs carry no org)**; miss → **RETRY** (send-race — DR-4);
  `set_org_context(message.org_id)`; insert event row (dup → DONE); `apply_event` per
  DR-5; stamp `error_code`/`segment_count_carrier`; `processed_at=now`; commit.
- `UnknownEvent` → dead_letter(`"unknown_event_type"`) → DONE.
- Any unexpected exception *after* the event row committed → set `processing_error`,
  leave `processed_at` NULL, still DONE (our `reprocess_pending` re-drives; returning 500
  would be useless — the dedupe row blocks the carrier's replay from reprocessing).

`reprocess_pending(session)` re-runs `process_event` for every row with
`processed_at IS NULL` — idempotent by the same state-machine rules; tested; scheduled
nowhere in P1 (P3+ worker).

### 6. Send path (`services/messaging.py`, `api/routes/messages.py`, `api/routes/numbers.py`)

`POST /api/v1/messages` (`require_permission("inbox:send")`), body
`{to, from?: str, body: str}`:
1. Normalize `to`/`from` with `phonenumbers` (region `"US"`), reject invalid → 422.
2. Resolve `from`: must be an **active `org_numbers` row of this org** (a scoped query —
   the session guard makes cross-org resolution structurally impossible); omitted → the
   org's single active number, else 422 (`ambiguous_from` in the message).
3. `check_outbound(...)` — deny → 422 `compliance_blocked`, **no row, no carrier call**.
4. `estimate(text)` → `segment_count_est`.
5. Thread upsert + create `Message(status="queued")`; **commit** (a racing DLR now RETRYs
   instead of vanishing).
6. `carrier.send_message(...)` → accepted: set `provider_message_id`, `status="accepted"`;
   `CarrierError`: `status="rejected"`, `error_code` = carrier code or category; commit.
7. **201 with the resource either way** (DR-7): `{id, thread_id, direction, status,
   from_e164, to_e164, body, segment_count_est, error_code}`.

Reads (`require_permission("inbox:read")`): `GET /api/v1/threads` (desc by
`last_message_at`, limit/offset), `GET /api/v1/messages?thread_id=` (asc by `created_at`),
`GET /api/v1/messages/{id}`. Numbers (P4 seed): `POST /api/v1/numbers`
(`numbers:manage`; body `{e164, carrier?}`; global-unique conflict → 409) and
`GET /api/v1/numbers` (`numbers:read`). All org-scoped; the P0 tenancy guard applies
unmodified.

### 7. Fixtures + FakeCarrier + live marker

- Fixtures are hand-constructed to the **exact** shapes in `docs/research/bandwidth.md`
  (array wrapper; `{time, type, to, description, message:{id, owner, applicationId, time,
  segmentCount, direction, to[], from, text, tag, media[], channel}}`; `message-failed`
  adds top-level `errorCode`). `batch-two-events.json` holds two events in one array —
  the array shape must be exercised, not simulated. In P1b, live captures land in
  `tests/fixtures/bandwidth/recorded/` and a diff against the constructed set is a gate
  item; a divergence is a P1b finding, fixed under Opus review.
- `conftest.py` additions: `load_fixture(name)`; `webhook_auth_headers()`; `FakeCarrier`
  (records every `OutboundMessage`, returns scripted `SendResult`s); an `org_with_number`
  helper (register → login → create org → POST a number); a `carrier` fixture installing
  `FakeCarrier` on `app.state`; and the `live_carrier` skip guard mirroring `pg_only`:
  skipped unless `BANDWIDTH_LIVE_TEST=1` **and** real credentials are present in the
  environment.
- `tests/live/test_live_roundtrip.py` (all `@pytest.mark.live_carrier`) runs at HTTP level
  against `SMOKE_BASE_URL` (the deployed instance — DLR webhooks need the public callback):
  outbound → poll the API until `delivered` (timeout 120 s, assert
  `segment_count_carrier` set); inbound half gated additionally on
  `BANDWIDTH_LIVE_INBOUND=1` (a human replies from the handset; poll for the inbound in
  the same thread). `scripts/smoke_sms.py` is the same flow as a human-readable script for
  the P1b gate demo.

### 8. Explicitly NOT in P1 (scope fence — reject any of these in review)

- **No frontend** of any kind, no inbox UI (P2). No contacts, no conversation assignment,
  no read-state, no sticky-sender logic (P2 — the thread bucket merely enables it).
- **No compliance logic** — no STOP/HELP parsing, no quiet hours, no DNC, no consent
  ledger (P3). The seam only.
- **No MMS pipeline** — no media fetch/re-host/S3 (P3). Inbound media URLs are stored as
  strings, period. Outbound `media` accepted by the domain object, not exposed on the API.
- **No voice, no BXML, nothing under a `voice/` path** (P5).
- **No Telnyx adapter, no failover** (P14). No number search/order/10DLC APIs (P4) —
  `org_numbers` rows are entered by hand via the minimal endpoint.
- **No campaigns, scheduling, pacing, or send retries** (P11). No queue, no Redis, no
  background workers — `reprocess_pending` exists as a function, unscheduled.
- No webhook endpoints for any carrier event class other than Bandwidth messaging.
- No dead-letter API surface, no admin UI for it.

## Test Spec

All local commands from `backend/` in the venv, on this Windows machine, today:

```
python -m pytest -q          # SQLite backend; pg_only + live_carrier skipped — must be green
python -m ruff check .       # must be clean (dialect ban + import hygiene)
```

Unit tests:
- [ ] `test_segments.py::test_gsm7_boundaries` → 160 chars→1, 161→2, 306→2, 307→3.
- [ ] `test_segments.py::test_ucs2_boundaries` → 70 emoji-bearing chars→1, 71→2, 134→2, 135→3.
- [ ] `test_segments.py::test_extension_chars_cost_two` → `"€"*80` → 1 segment; `"€"*81` → 2.
- [ ] `test_message_state_machine.py::test_transition_table` → parametrized over every
      (current, event) pair asserting exactly the DR-5 outcomes.
- [ ] `test_message_state_machine.py::test_delivered_before_sending` → queued→accepted→
      apply delivered → `delivered`; apply sending → returns None (no regression).
- [ ] `test_message_state_machine.py::test_terminal_immutable` → failed after delivered
      (and the reverse) → no change, and the conflict is signalled for WARN logging.
- [ ] `test_bandwidth_adapter.py::test_send_success` → MockTransport asserts URL contains
      the account id, Basic auth header present, body has `to` as a LIST, `applicationId`,
      `tag`; 202 fixture → `SendResult("accepted", <id>, None)`.
- [ ] `test_bandwidth_adapter.py::test_error_classification` → parametrized: 400+`4720`→
      `invalid_request` non-retryable; 400+`4476`→`unregistered`; 429→`rate_limited`
      retryable; 503→`carrier_transient` retryable; `httpx.ConnectError`→
      `carrier_unreachable` retryable; 401→`auth`.
- [ ] `test_bandwidth_adapter.py::test_capabilities_honest` → `supports_cancel is False`,
      `sync_delivery_status is False`.
- [ ] `test_webhook_ingest.py::test_verify_constant_time_shape` → wrong password → 401;
      wrong username → 401; both wrong → 401; missing header → 401 — identical bodies, and
      zero rows written in all four cases.

Integration tests (httpx `AsyncClient` against the app factory, SQLite):
- [ ] `test_send_api.py::test_send_happy_path` → FakeCarrier; 201, `status=="accepted"`,
      `provider_message_id` stored, thread created, `tag == str(message id)` captured by
      the fake, `segment_count_est` correct, omitted `from` resolved to the org's sole
      number.
- [ ] `test_send_api.py::test_from_number_enforced` → a `from` the org does not own → 422
      **including a number owned by another org** (the tenancy teeth); invalid `to` → 422.
- [ ] `test_send_api.py::test_carrier_rejection_is_data` → fake returns
      `CarrierError(4720)` → HTTP **201**, `status=="rejected"`, `error_code=="4720"`.
- [ ] `test_send_api.py::test_no_carrier_is_503` → `app.state.carrier=None` → 503
      `carrier_not_configured`, zero message rows.
- [ ] `test_send_api.py::test_tenancy_on_reads` → org A sends; org B's `GET /threads`,
      `GET /messages?...`, `GET /messages/{a_id}` see nothing (empty / empty / 404).
- [ ] `test_send_api.py::test_permission_denied_without_inbox_send` → a role lacking
      `inbox:send` (created directly via repository in the test) → 403.
- [ ] `test_webhook_ingest.py::test_array_batch` → `batch-two-events.json` → 200, two
      ledger rows, both messages transitioned.
- [ ] `test_webhook_ingest.py::test_inbound_creates_message_and_thread` →
      `message-received` fixture aimed at a seeded org number → 200; message
      `direction=="inbound"`, `status=="received"`, correct org, thread upserted; posting
      the identical payload again → 200 and still exactly one message, one event row.
- [ ] `test_webhook_ingest.py::test_inbound_unknown_number_dead_letters` → 200, zero
      messages, one `webhook_dead_letters` row with reason `unknown_number`.
- [ ] `test_webhook_ingest.py::test_dlr_unknown_id_returns_500_then_succeeds` → DLR for an
      unknown id → **500**, nothing persisted; create the message (simulating the send
      commit landing); replay the same DLR → 200, transition applied. This is the
      send-race contract.
- [ ] `test_webhook_ingest.py::test_malformed_body_dead_letters` → non-JSON and
      JSON-non-array both → 200 + dead letter, nothing else.
- [ ] `test_webhook_ingest.py::test_failed_event_records_error_code` → `message-failed`
      fixture (errorCode 4720) → status `failed`, `error_code=="4720"`.
- [ ] `test_webhook_ingest.py::test_reprocess_pending_is_idempotent` → an event row with
      `processed_at NULL` → `reprocess_pending` applies it once; running again changes
      nothing.
- [ ] `test_webhook_ingest.py::test_replay_out_of_order_3x` — **THE P1a GATE TEST**: send
      via FakeCarrier, then POST the DLR sequence `[delivered, sending, delivered,
      sending, delivered, sending]` as six separate webhook calls (each event also
      appearing once inside an array batch) → final `status=="delivered"`, exactly **2**
      event rows (`sending`, `delivered`), `segment_count_carrier` set; the resulting
      `messages` + `message_events` rows are field-for-field identical to a control run
      that received `sending` then `delivered` once each.
- [ ] `test_compliance_seam.py::test_gate_called_exactly_once_and_deny_blocks` → spy on
      `check_outbound`: happy send calls it once with the draft; stubbed deny → 422
      `compliance_blocked`, **zero** message rows, **zero** FakeCarrier calls; and
      `on_inbound` is invoked once per ingested inbound.
- [ ] (`pg_only`) `test_webhook_ingest.py::test_concurrent_duplicate_ingest_pg` → two
      truly concurrent posts of the same event against Postgres → one event row, one
      transition (the `IntegrityError` path under real concurrency).
- [ ] (`live_carrier`, P1b) `tests/live/test_live_roundtrip.py` → real send reaches
      `delivered` with `segment_count_carrier`; (inbound-gated) a handset reply lands in
      the same thread.

Manual verification:
- [ ] P1a: `python -m pytest -q` and `ruff check` clean locally on 3.10; CI: all three P0
      jobs green — `test-postgres` (now including the new suite + `pg_only` concurrency
      test) remains the merge gate.
- [ ] P1a: `uvicorn` locally with SQLite; create org + number; POST a message with
      `BANDWIDTH_ENABLED=false` → observe the 503; POST the `message-received` fixture at
      the webhook with curl + Basic auth → see it in `GET /threads`.
- [ ] P1b: R1 credentials in the VPS `.env` (operator); nginx exposes exactly the webhook
      path per `deploy/nginx-csaas.conf` (operator-supervised); Bandwidth dashboard
      callback URL + Basic credentials set; `python scripts/smoke_sms.py` completes the
      round-trip; live payloads captured into `tests/fixtures/bandwidth/recorded/` and
      diffed against the constructed fixtures — divergences resolved before sign-off.
- [ ] `docs/PROGRESS.md` updated: P1a status on completion; P1b status + R1 outcome on
      sign-off.

Pass criteria — **P1a:** ALL unit + integration tests green locally on SQLite/3.10 AND on
CI's Postgres job; ruff clean. Commit `feat(phase-1a): carrier layer, webhook ingestion,
message state machine, send API (fixture-verified)`. **P1b:** the live round-trip
demonstrated per the gate above. Commit `feat(phase-1b): live Bandwidth round-trip
verified`. P2 may start on P1a sign-off; P1b completes whenever R1 does.

## Deploy

**P1a: no.** Nothing new runs on the VPS; there is nothing live to receive.
**P1b: yes — operator-supervised.** The P0 skeleton deploy (api on 127.0.0.1:8080) plus:
nginx TLS server block from `deploy/nginx-csaas.conf` proxying **only**
`/api/v1/webhooks/bandwidth/messaging` (and `/healthz`) to 127.0.0.1:8080 — the rest of
the API stays unexposed until P2 needs it; certbot for the chosen hostname; Bandwidth
credentials added to `/opt/csaas/.env` by the operator, never by a script. All P0 VPS
constraints restated: everything under `/opt/csaas`, no global installs, no main-config
nginx edits (a self-contained site file only), pre-flight before anything starts.
