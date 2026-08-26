# Phase 3 — MMS, templates, and the compliance core

> Refined by Fable (Tier-1) 2026-08-26. Workstreams: WS-2 (Messaging), WS-8 (Compliance).
> Builds on P0 + P1a + P2a (139 backend + 17 frontend tests green, all 5 CI jobs green,
> session-layer tenancy, carrier layer + ingestion, contacts/inbox console, loopback carrier).
> **D7 is the heart of this phase: compliance is a first-class module, enforced centrally.**
> There is no OSS TCPA/DNC library in any language (oss-headstart.md) — we build it here.
> **R1 is still open — no Bandwidth account.** Same split as P1/P2: **P3a** is the entire
> codebase of the phase, buildable and demonstrable today on Python 3.10 + SQLite via the
> loopback carrier and fixture-driven tests; **P3b** (live MMS round-trip with a handset,
> live STOP from a real phone) adds zero production code and is blocked on R1/P1b.

## Goal

Fill in the compliance seam P1 cut and P2 pinned: an append-only consent ledger keyed on
`(org_id, contact_e164)` so **STOP suppresses the whole number pool** (gotcha #1), a
whole-message keyword engine (STOP/START/HELP families) that structurally cannot repeat
the prior production incident where a STOP-footer regex DNC'd legitimate buyers, quiet
hours evaluated **in the recipient's timezone with real DST handling** (gotcha #2) that
**defer** rather than drop, an internal DNC suppression list with an import-usable scrub
function and an honest un-mistakable federal-DNC stub, and the legally required
auto-replies. Plus the phase's product surface: an MMS media pipeline (upload → validate
→ store → signed URLs → carrier; inbound media fetched **outside** the webhook path and
re-hosted before Bandwidth's ~48 h hosting lapses — gotcha #3), and message templates with
merge fields. Storage rides a new `ObjectStore` abstraction with filesystem and in-memory
backends that run today with no MinIO and no Docker.

**P3a gate (today, no carrier):** full backend suite green on 3.10 + SQLite and on the
Postgres CI job; frontend jobs green; and a loopback demo: (1) STOP sent to number A →
a send from number B **in the same org** is 422 `compliance_blocked` with an audit row;
the auto-reply confirmation went out despite the opt-out; a body *containing* "reply STOP
to unsubscribe" did **not** opt anyone out; (2) a send aimed into the recipient's local
21:30 is **held** (201, `status="queued"`, `hold_until` set) and released by the sweeper
when the clock (frozen in tests) passes 08:00 recipient-local — unless the contact opted
out while held, in which case it dies rejected; (3) an MMS composed with an uploaded image
round-trips through loopback, the inbound copy is fetched by the media worker into the
object store, and the inbox renders it.

**P3b gate (blocked on R1/P1b):** PHASES.md's P3 gate live — real handset: STOP to number
A blocks number B; a scheduled-into-quiet-hours send defers; MMS round-trips with media
stored and rendered; recorded live MMS webhook payloads committed as fixtures and diffed
against constructed ones.

---

## Decision Record (settled for P3 — do not relitigate in implementation)

### DR-1: Storage — `ObjectStore` protocol; filesystem + in-memory backends now, S3 in P5

**The honest constraint:** S3/MinIO is configured in settings but nothing runs locally —
no Docker, no MinIO, no Postgres. An S3 backend written today would be untestable dead
code (or drag boto3+moto for code no phase exercises until recordings exist). So:

- `app/storage/base.py` defines the seam:
  ```python
  class ObjectStore(Protocol):
      name: str
      async def put(self, key: str, data: bytes, content_type: str) -> None: ...
      async def get(self, key: str) -> bytes: ...          # KeyError if absent
      async def exists(self, key: str) -> bool: ...
      async def delete(self, key: str) -> None: ...        # idempotent
  ```
  Keys are opaque strings; P3 always uses `org/{org_id}/media/{asset_id}` — tenant-prefixed
  by construction, which is also the P13 metering and retention-tiering hook (gotcha #7:
  plan lifecycle on day one — the key layout and the `expires_at` column below are that plan).
- **`LocalFSObjectStore`** (default): writes under `settings.media_local_root`
  (default `var/media/`, git-ignored), path-traversal-proofed (key is validated
  `^[A-Za-z0-9/_\-\.]+$` and resolved paths must stay under the root). Files are ≤3.75 MB,
  so writes go through `asyncio.to_thread` with plain `open` — **no aiofiles dep**. This is
  a *production-capable* backend for the single-VPS deploy, not a fake.
- **`InMemoryObjectStore`**: a dict, for tests that don't want tmp dirs.
- `build_store(settings)`: `media_store_backend` ∈ `local | memory | s3`; **`s3` raises
  `ConfigurationError("S3 object store backend arrives in P5 (recordings); use 'local'")`**
  — configuring S3 today fails loudly at boot instead of silently half-working. The
  existing `S3_*` settings fields are untouched and unconsumed; P5 implements the backend
  behind this same protocol and nothing above the seam changes.

**URLs, and why signing exists:** two consumers need media bytes without a JWT —
(a) Bandwidth, which fetches outbound MMS from a URL array, and (b) the browser's
`<img src>`, which cannot send an `Authorization` header. One endpoint serves both:
`GET /api/v1/media/{asset_id}/content?exp=<unix>&sig=<hmac>` where
`sig = HMAC-SHA256(key=jwt_secret, msg=f"media:{asset_id}:{exp}")` (constant-time
compare, expired → 403, tampered → 403). The service mints **72 h** URLs for the carrier
(it may retry) and **15 min** URLs for API responses the console renders. No new secret:
the signing key is derived from `jwt_secret` with the `media:` context prefix. A valid
JWT + org scope also works on the same endpoint (inbox convenience), but signed access
never leaks org data beyond the single asset it names.

### DR-2: Inbound media fetching stays OUT of the webhook request path

The ingestion path is DB-only and 2xx-fast (D6) and P3 does not change that. The first
outbound network call in the inbound pipeline is quarantined like this:

1. `_ingest_inbound` (surgical edit) creates one `media_assets` row per inbound media URL
   — `status="pending"`, `source_url` = the carrier URL — **inside the existing deduped
   transaction**, so a replayed webhook rolls the asset rows back with everything else and
   cannot double-create them. `messages.media` keeps the raw carrier URLs exactly as in
   P1 (audit truth). Zero HTTP happens here.
2. Fetching is `fetch_pending_media(session, http_client, store, carrier)` in
   `app/services/media.py`: select assets with `status="pending"` and
   `next_attempt_at <= now` (allow_unscoped select then `set_org_context` per row — the
   same justified pattern as `reprocess_pending`), stream-download with a hard 3.75 MB cap
   (abort mid-stream when exceeded → `too_large`), content-type from the response header
   validated against the allowlist (→ `unsupported`), sha256 recorded, bytes `put` to the
   store, `status="stored"`. Failures increment `fetch_attempts`, set `last_error` and
   `next_attempt_at = now + 2^attempts minutes`; after 6 attempts → `status="failed"` +
   ERROR log (the carrier URL dies at ~48 h — this log line is the alarm).
   **Bandwidth-hosted media requires Basic auth**: the `MessagingCarrier` protocol gains
   `def media_auth(self, url: str) -> tuple[str, str] | None` (default `None`);
   the Bandwidth adapter returns its API credentials iff the URL host ends with
   `bandwidth.com`; loopback returns `None`. Credentials are never sent to foreign hosts.
3. **Scheduling without a scheduler (P11 owns the real one):** after the webhook response
   is sent and the transaction committed, the route spawns a fire-and-forget
   `asyncio.create_task` (exceptions logged, never raised — the loopback pattern) that
   runs one fetch pass. Belt-and-braces: the lifespan **sweeper** (DR-5) re-runs
   `fetch_pending_media` every interval, so a crashed task or a restart loses nothing —
   `pending` rows in the DB are the queue.
   Tests never rely on the background task: they call `fetch_pending_media` directly with
   an `httpx.AsyncClient(transport=MockTransport(...))` — deterministic, no network, no
   sleeps. A dedicated test proves the webhook returns 200 with **zero** HTTP calls made
   during the request (the mock transport's call log is empty until the fetch function
   runs).

**Outbound MMS:** `POST /api/v1/media` (multipart, `inbox:send`) validates size ≤3.75 MB
and content-type allowlist (`image/jpeg image/png image/gif image/webp video/mp4
video/3gpp audio/mpeg text/vcard application/pdf`), stores, returns the asset. `SendIn`
gains `media_ids: list[uuid] = []` (≤10); the send service verifies every id is this
org's `stored` asset (scoped query; anything else → 422), passes 72 h signed URLs to
`OutboundMessage.media`, and links assets to the message. **No transcoding/downres in
v1** — carriers downres silently anyway (gotcha #3); we enforce the hard cap and record
what we sent; "delivered" for MMS is still only a DLR claim (gotcha #4 — awareness, P13
analytics owns detection).

### DR-3: Opt-out — append-only consent ledger keyed `(org_id, contact_e164)`; keyword = whole message only

**The ledger** (`consent_events`, TenantScoped, **append-only** — no UPDATE/DELETE code
path exists, no PATCH/DELETE API exists, review rejects any):
`contact_e164`, `channel` (`"sms"` now — `"voice"` is P5's, see DR-7), `event` ∈
`opt_out | opt_in | dnc_add | dnc_remove`, `source` ∈ `keyword | manual | import | api`,
`keyword_matched` nullable, `message_id` nullable FK **with a unique constraint on
`message_id`** (NULLs distinct on both dialects — the same trick as
`messages.provider_message_id`), `actor_user_id` nullable, `details` PortableJSON.
The unique `message_id` makes keyword processing idempotent under webhook replay at the
constraint level, not by application check.
**Current state is derived, never denormalized:** latest `opt_out`/`opt_in` event per
`(org_id, contact_e164, channel)` wins — one indexed query
(`ix_consent_org_e164_channel_created` on `(org_id, contact_e164, channel, created_at)`),
`ORDER BY created_at DESC, id DESC LIMIT 1`. A flag column would drift; a derived read
cannot (the D6 principle applied to consent).

**Whole-pool suppression is structural, not procedural:** the key contains no `our_e164`
anywhere — there is nothing per-number to check, so gotcha #1 cannot regress. The gate
receives `(org_id, draft.to_e164)`, exactly this key (the P2 DR-4 invariant, now cashed
in).

**The keyword engine** (`app/compliance/keywords.py`, pure):
`classify_keyword(text) -> KeywordHit | None` where the hit is `("opt_out"|"opt_in"|
"help", matched_word)`. Normalization: strip whitespace, strip **trailing** punctuation
(`. ! ,`), casefold. Then **the entire remaining message must equal one keyword**:
opt-out `STOP STOPALL UNSUBSCRIBE CANCEL END QUIT`, opt-in `START YES UNSTOP`,
help `HELP INFO`. **Never substring, never regex-in-body, never "contains".** This is the
designed-in guard against the prior production incident in our history where a
STOP-footer regex matched marketing footers ("Reply STOP to unsubscribe") inside
legitimate buyers' messages and DNC'd them: under this engine that body is a multi-word
message and classifies as `None`, pinned by regression tests
(`"please stop texting me"`, `"I want to stop"`, `"Reply STOP to unsubscribe"`,
`"Yes I'm interested in the house"` → all `None`; `"STOP"`, `" stop. "`, `"Unsubscribe!"`
→ hits). A conversational refusal that doesn't match is an operator/AI concern (P10/P13
can flag); the manual opt-out endpoint covers it today.

**Auto-replies (legally required by CTIA/TCPA practice), and how they pass the gate:**
- STOP → one confirmation ("You are unsubscribed from {org} messages. No more messages
  will be sent. Reply START to resubscribe. Reply HELP for help.") — the single permitted
  post-opt-out send, no marketing content.
- HELP → org help text ("{org}: for help contact {help_contact}. Msg&data rates may
  apply. Reply STOP to unsubscribe.") — must answer even for opted-out contacts.
- START → opt-in confirmation.
  Texts live in `compliance_settings` (DR-4) with `{org}`/`{help_contact}` interpolation;
  sent **from the number that received the keyword** (no sticky lookup — it must come
  from the number they texted).
  Mechanism: `check_outbound` gains a **keyword-only** parameter
  `exemption: str | None = None`; the only accepted value is `"compliance_auto_reply"`
  and only `app/compliance/service.py` passes it. An exempted send skips all three
  checks — opt-out, DNC, and quiet hours (a STOP confirmation must reach someone who
  just texted us, DNC-listed or not) — but it still flows through the one choke point
  and is still audited: the verdict records the exemption. The seam's P1/P2 spy tests
  keep passing because the parameter is keyword-only with a default.
- **Replay:** `on_inbound` fires once per unique inbound (pinned by the existing seam
  test); the `message_id` unique constraint on the ledger makes even a double-fire
  harmless — the second insert IntegrityErrors → no event, no second auto-reply.

**Manual overrides:** `POST /compliance/optout` (manual, any time). `POST
/compliance/optin` is **refused with 409 when the latest opt-out event has
`source="keyword"`** — only the consumer's own START reverses a keyword STOP; an
operator "fixing" a STOP is exactly the TCPA lawsuit shape. Manual opt-in is allowed
when the latest opt-out was `manual`/`import`.

**In-flight sends:** P3 has no campaign queue — the only queue-like state is DR-5's held
messages, and those **re-run the full gate at release**, so an opt-out that lands during
the hold kills the held send (`rejected`, `error_code="opted_out_while_held"`). The
contract P11 must inherit is stated here once: **the gate runs at dispatch time, never
only at enqueue time.**

### DR-4: Quiet hours — recipient timezone, DST-correct, DEFER not reject

**Resolution order** for a recipient's timezone (`app/compliance/quiet_hours.py`):
1. `contacts.timezone` (new nullable String(64), IANA name, validated against `zoneinfo`
   on write) — the explicit field always wins; it exists precisely because **area code ≠
   location** (D7, gotcha #12's cousin).
2. NPA (area code) → timezone table: `app/compliance/npa_tz.py`, a committed static dict
   `NPA_TZ: dict[str, tuple[str, ...]]` covering US/CA NPAs, values are **tuples** because
   some NPAs span zones. Source: NANPA public data; the implementer ships the full table
   plus (a) a structural test that every value loads in `zoneinfo` and (b) spot-checks
   (212→America/New_York, 214/469/972→America/Chicago, 415→America/Los_Angeles,
   602→America/Phoenix [no DST], 808→Pacific/Honolulu, 907→America/Anchorage).
3. Unknown NPA / non-US number → the **all-US conservative set**
   (`America/New_York`, `America/Chicago`, `America/Denver`, `America/Phoenix`,
   `America/Los_Angeles`, `America/Anchorage`, `Pacific/Honolulu`).

**The rule:** a send is allowed only if the local time is inside
`[window_start, window_end)` — the **allowed** sending window, default **08:00–21:00**,
the federal TCPA floor — **in every candidate zone**. Multi-zone NPAs and the unknown
fallback therefore fail SAFE: an incomplete table can only make us *more* restrictive,
never send at 3 a.m. DST is free because evaluation converts the aware UTC instant into
each zone with `zoneinfo` at that instant — no offset arithmetic anywhere. **New runtime
dep `tzdata` is mandatory:** Windows (this dev machine) and slim containers have no
system tz database; without it `ZoneInfo("America/Chicago")` raises.

**Defer, not reject — and why:** PHASES.md's P3 gate says "deferred, not dropped", and
defer is also the only answer P11 can build on (a reject pushes retry logic into every
future caller). Since P11 owns the real scheduler, P3 ships the minimal honest version:
- `evaluate(to_e164, contact_tz, settings, now) -> Ok | Deferred(not_before)` — pure,
  `now` injectable; `not_before` = the earliest instant that is ≥08:00 local in **all**
  candidate zones (computed per zone, take max).
- `ComplianceVerdict` gains `defer_until: datetime | None = None` (frozen dataclass,
  additive — every existing constructor call and spy stays valid).
- Send path on defer: create the message row `status="queued"` with new column
  `messages.hold_until` (tz DateTime, nullable), **skip the carrier**, 201 with the
  resource (`hold_until` serialized) — consistent with DR-7-of-P1's "one uniform
  resource" philosophy.
- Release: `release_held_messages(session, carrier, now=None)` in
  `services/messaging.py` — selects queued messages with `hold_until <= now`
  (allow_unscoped then `set_org_context` per row), **re-runs the full gate**, then
  dispatches through the same `_dispatch_to_carrier(...)` helper factored out of
  `send_message` (accepted/rejected handling identical). Gate deny → `rejected` +
  `error_code="opted_out_while_held"` (or `dnc_while_held`); gate re-defer (released at
  a boundary that's still quiet in some candidate zone) → update `hold_until`, keep
  waiting.
- **Conversational carve-out:** quiet hours are a telemarketing rule; blocking a human
  agent's 9:05 p.m. reply to a customer who texted at 9:00 p.m. would make the inbox
  unusable while adding zero legal safety. The quiet-hours check is skipped when the
  contact has an **inbound message in this org within the last 24 h** (one indexed
  query). Campaigns (P11) always hit the gate cold, so bulk traffic never rides this.
  Documented as org-invariant in v1; configurable later if ever needed.

**Gate order inside `check_outbound`:** 1 opt-out ledger → 2 internal DNC → 3 quiet
hours. Deny beats defer; each verdict carries a stable reason code (`opted_out`, `dnc`,
`quiet_hours`). On **deny** the gate itself writes and **commits** a `compliance_blocks`
audit row (org_id, to/from e164, reason, body truncated to 255, exemption if any) before
returning — the gate is called before any other row exists, so the commit is safe, and
the row survives the `ComplianceBlockedError` the caller then raises (P1 DR-8 assigned
this audit ledger to P3; the seam tests only assert zero `messages` rows, which stays
true).

**Test determinism (critical — CI runs at arbitrary wall times):** with real compliance
logic, P1/P2 send tests would pass or fail depending on the hour CI runs. Fix without
touching P1/P2 test files: `quiet_hours.py` reads time via module-level `_now()`;
`conftest.py` gains an **autouse fixture** monkeypatching it to a fixed
`2026-06-15T18:00:00Z` (13:00 CDT / 14:00 EDT / 08:00 HST — inside the allowed window
for every candidate zone used by fixtures). P3's own quiet-hours tests pass explicit
`now=` values (including DST-transition instants: 2026-03-08 and 2026-11-01 US
changeovers) or re-patch locally.

### DR-5: One in-process sweeper, explicitly interim until P11

Three functions now need periodic driving: `fetch_pending_media`,
`release_held_messages`, `purge_expired_media` (below), plus P1's dormant
`reprocess_pending`. P3 adds `app/services/sweeper.py`: a single asyncio loop started in
lifespan (`sweeper_enabled: bool = True`, `sweeper_interval_seconds: int = 60` in
settings; conftest builds apps with it disabled), each pass running the four functions in
its own sessions, every exception logged and swallowed (the loop must never die).
**This is scheduler-lite and it is throwaway by design:** the *functions* are the seam
P11's real scheduler (Redis-driven) will call; the loop is 30 lines we delete then. No
Redis in P3.

**Media retention (gotcha #7 down-payment):** `media_assets.expires_at` (tz, nullable),
stamped at store time from `media_retention_days` (settings, default 0 = never expire).
`purge_expired_media(session, store)` deletes store object + flips status to `purged`
(row kept — audit). Small, tested, wired into the sweeper; real lifecycle *tiering* is
P13's.

### DR-6: DNC — internal list real, federal honestly absent

- `dnc_entries` (TenantScoped): `e164`, `source` (`manual | import | complaint`),
  `reason` String(255) nullable, `added_by_user_id` nullable, unique `(org_id, e164)`.
  CRUD via API (`compliance:manage`); every add/remove **also appends a
  `consent_events` row** (`dnc_add`/`dnc_remove`) so the append-only ledger is the one
  complete audit trail even though the working table is mutable.
- `scrub(session, org_id, numbers) -> list[ScrubResult]` in `compliance/service.py`:
  per-number `ScrubResult(e164, ok, reasons: list[str], federal_checked: bool)` checking
  opt-out state + internal DNC. **This function is P11's import-time scrub** — P3 builds
  it org-scoped and side-effect-free; P11 calls it per CSV row. A thin
  `POST /api/v1/compliance/scrub {numbers: [...]}` (≤500 per call, `compliance:read`)
  exposes it for manual checks and exercises it end-to-end.
- **Federal DNC:** we have no SAN subscription and no OSS library exists. The stub is
  designed to be impossible to mistake for scrubbing: `federal_checked` is a field on
  **every** `ScrubResult` and is always `false`; the reasons list always contains
  `"federal_dnc:unchecked"`; `provider_statuses()` gains a `federal_dnc` entry reporting
  `enabled=False, reason="no registry subscription — numbers are NOT scrubbed against
  the federal DNC"` (logged at boot like every other disabled provider); and there is
  **no settings flag** that could claim otherwise — a real integration is a future paid
  decision (SAN / TCPA Litigator List) that lands behind a `FederalDncChecker` protocol
  slot in `scrub`'s signature, added then, not now. The gate does **not** consult federal
  DNC (it cannot); it never pretends to.

### DR-7: The ledger is channel-shaped so P5 hangs recording consent off it, unchanged

`channel` is a column, not an afterthought: P5 appends
`consent_events(channel="voice", event="opt_in", source="api",
details={"kind": "recording", "state": "...", "call_id": "..."})` rows through the same
append-only table, same derived-latest-wins read, same `(org_id, contact_e164)` key.
What P5 adds is *additive only*: possibly a nullable `call_id` column and the
two-party-consent state table (`TWO_PARTY_CONSENT_STATES` + number→state resolution) —
**explicitly NOT built in P3** (the state data and "area code ≠ physical location"
enforcement are call-time concerns). P3's obligation is only that nothing here assumes
`channel == "sms"`: the gate queries filter on channel, the API exposes it, tests cover
a voice-channel row being invisible to the SMS gate.

### DR-8: Templates — merge fields without a template engine

`message_templates` (TenantScoped): `name` String(127) (unique `(org_id, name)`),
`body` Text, `media_asset_ids` PortableJSON default list. Renderer
(`services/templates.py`): a ~30-line `{{path}}` token substituter over an allowlisted
namespace — `contact.first_name`, `contact.last_name`, `contact.display_name`,
`contact.attributes.<key>` (validated against `custom_field_defs`), `org.name`. **No
Jinja** — a real template engine is an SSTI surface and a dependency for nothing v1
needs. Unknown token → 422 (fail loud at render, not at send); known-but-empty value →
empty string, with the token listed in the response's `warnings`. Endpoints:
CRUD (`templates:manage`), list/get (`templates:read` — agents compose with them),
`POST /templates/{id}/render {contact_id}` → `{body, media_asset_ids, warnings}`. The
composer renders client-side-visible text first, then sends through the normal send API
— templates never bypass the gate because they never touch the carrier themselves.

### DR-9: RBAC + settings additions (P2 DR-9 pattern)

New permission keys: `compliance:read` (view consent/DNC/scrub — owner, admin, agent),
`compliance:manage` (opt-out/opt-in/DNC edits, settings — owner, admin),
`templates:read` (owner, admin, agent), `templates:manage` (owner, admin). Migration
`0004` re-seeds `is_system` roles by name from literals inlined in the migration
(idempotent, non-system roles untouched — the established 0003 mechanism).
`compliance_settings` (TenantScoped, one row per org, lazily created with defaults on
first read): `window_start`/`window_end` String(5) "HH:MM" — the **allowed** sending
window in recipient-local time, named to avoid the classic quiet-hours inversion bug
(defaults "08:00"/"21:00", validation **clamps inside the federal floor** — an org may
narrow the window, never widen it), `help_contact` String(255), `optout_text`/`optin_text`/`help_text`
(Text, defaulted templates with `{org}`/`{help_contact}` interpolation).
Settings additions (surgical, `config.py`): `media_store_backend` ("local"),
`media_local_root` ("var/media"), `media_retention_days` (0), `sweeper_enabled` (True),
`sweeper_interval_seconds` (60). No new secrets.

### DR-10: Surgical-edit budget on frozen files

`gate.py` stops being a stub — that is the phase. `messaging.py` gains: media-asset rows
in `_ingest_inbound`, defer path + `_dispatch_to_carrier` factoring + `media` handling in
`send_message`, `release_held_messages`. The seam's **signature** changes only
additively (keyword-only `exemption`); `test_compliance_seam.py` is **not modified** and
must stay green as-is — it is the proof the seam held. The P2 loopback carrier gains
media echo (outbound media URLs echoed as inbound `media` so the demo exercises the
fetch pipeline against our own signed URLs). Anything beyond the scopes listed in
Allowed Files is a guardrail violation.

---

## Pre-dependencies

- P0 + P1a + P2a merged; suite green (139 backend + 17 frontend baseline).
- Backend deps added to `backend/pyproject.toml`: **`tzdata`** (zoneinfo data — mandatory
  on Windows and slim containers) and **`python-multipart`** (FastAPI multipart uploads
  — add only if not already a transitive dependency of the template fork; check first).
  **Nothing else** — no boto3/aioboto3/moto (S3 is P5), no aiofiles, no Jinja, no Pillow,
  no Redis client.
- Frontend: no new deps.
- Dev `.env`: nothing new required (media defaults to `var/media/`; loopback stays on).
  `.env.example` gains the five DR-9 settings lines — **Fable applies that edit**, the
  implementer does not touch it.
- **P3b only (user-owned):** R1 resolved; P1b/P2b deployed; a handset for the live STOP
  and MMS demo.

## Allowed Files (implementer may create/read/write — nothing else)

New backend files:
```
backend/app/storage/__init__.py
backend/app/storage/base.py                 (protocol + LocalFS + InMemory + build_store)
backend/app/compliance/keywords.py
backend/app/compliance/quiet_hours.py
backend/app/compliance/npa_tz.py            (static NPA→zones table + docstring naming the source)
backend/app/compliance/service.py           (ledger ops, DNC, scrub, settings, auto-replies)
backend/app/models/compliance.py            (ConsentEvent, DncEntry, ComplianceBlock, ComplianceSettings)
backend/app/models/media.py                 (MediaAsset)
backend/app/services/media.py               (upload/validate/sign, fetch_pending_media, purge_expired_media)
backend/app/services/templates.py
backend/app/services/sweeper.py
backend/app/api/routes/media.py             (upload, content endpoint with sig-or-JWT)
backend/app/api/routes/compliance.py        (consent, optout/optin, dnc, scrub, settings)
backend/app/api/routes/templates.py
backend/migrations/versions/0004_compliance_media.py
backend/tests/test_keywords.py
backend/tests/test_quiet_hours.py           (includes npa_tz structural + spot checks + DST)
backend/tests/test_optout_engine.py
backend/tests/test_dnc.py
backend/tests/test_object_store.py
backend/tests/test_media_pipeline.py
backend/tests/test_templates.py
backend/tests/test_held_messages.py
```
Surgical edits ONLY, scope stated — nothing else in these files moves:
```
backend/pyproject.toml                      (add tzdata, python-multipart if absent)
backend/app/compliance/gate.py              (REPLACE stub bodies with DR-3/DR-4 logic; extend
                                             ComplianceVerdict additively; keyword-only exemption)
backend/app/config.py                       (the five DR-9 fields; nothing else)
backend/app/errors.py                       (nothing expected; if a new class proves necessary, STOP and report)
backend/app/models/messaging.py             (Message: add hold_until; add MessageTemplate table; nothing else)
backend/app/models/contacts.py              (Contact: add timezone column; nothing else)
backend/app/models/rbac.py                  (add the four DR-9 permission keys to PERMISSIONS
                                             + system-role lists; nothing else)
backend/app/models/__init__.py              (export new models)
backend/app/providers/domain.py             (protocol: add media_auth default-None; nothing else)
backend/app/providers/base.py               (build_store is NOT here — no change unless get_carrier
                                             typing needs the new method; prefer no change)
backend/app/providers/bandwidth/adapter.py  (media in send payload if not already wired; media_auth)
backend/app/providers/loopback.py           (echo media URLs on the inbound echo; media_auth=None)
backend/app/services/messaging.py           (per DR-10: asset rows in _ingest_inbound, defer path,
                                             _dispatch_to_carrier factoring, release_held_messages;
                                             every P1/P2 ingestion/dedupe contract identical)
backend/app/api/routes/messages.py          (SendIn: media_ids; MessageOut: hold_until + media
                                             [{id, content_type, url}]; GET /messages joins assets
                                             with ONE extra IN-query; nothing else)
backend/app/api/routes/contacts.py          (accept/validate contacts.timezone; nothing else)
backend/app/main.py                         (mount three routers; store in lifespan/app.state;
                                             sweeper start/stop; nothing else)
backend/tests/conftest.py                   (autouse frozen-clock fixture per DR-4; media/store/
                                             compliance helpers; sweeper_enabled=False in test
                                             settings; P0-P2 fixtures untouched)
backend/tests/test_inbox_aggregate.py       (ONLY if the optional media_count preview query is
                                             added: ceiling stays ≤8; otherwise untouched)
frontend/src/**  + frontend/openapi.json    (composer attachments + template picker, MessageBubble
                                             media rendering, thread opt-out banner + disabled
                                             composer, CompliancePage [settings, DNC, scrub],
                                             regenerated types; tests colocated)
docs/PROGRESS.md                            (status updates only)
```

## Forbidden (implementer must never touch)

- `.env` / `.env.example` (Fable applies the settings lines) / any secrets, local or VPS.
- `docs/` other than `docs/PROGRESS.md`.
- **`backend/tests/test_compliance_seam.py` — frozen.** It must pass unmodified; that is
  the evidence the seam contract held. Ditto every other P0–P2 test file not named above.
- All other P0–P2 modules — in particular `app/db/*`, `app/auth/*`,
  `app/providers/bandwidth/webhooks.py`, `app/providers/segments.py`,
  `app/services/sender.py`, `app/services/contacts.py`, `app/services/inbox.py`,
  migrations `0001`–`0003`, `.github/workflows/ci.yml` (the five jobs already cover P3 —
  no CI edits). If P3 seems to need a change there, STOP and report.
- The ingestion contract: outcomes (DONE/RETRY/DEAD_LETTER), the idempotency constraints,
  the state-machine table, the webhook route's DB-only request path. P3 adds
  transactional side effects inside the existing deduped transaction and *post-response*
  work only.
- The append-only ledger: no UPDATE or DELETE statement may target `consent_events`,
  ever, including in tests' cleanup.
- Anything on the VPS (P3a deploys nothing; P3b is operator-supervised).
- No new deps beyond `tzdata` (+ `python-multipart` if absent). No S3/boto code paths, no
  Redis, no Jinja, no Pillow, no freezegun (inject `now`), no new frontend deps.

## Implementation Notes

### 1. Schema (migration `0004_compliance_media` — all additive)

- **consent_events** / **dnc_entries** / **compliance_blocks** / **compliance_settings**
  per DR-3/DR-6/DR-4/DR-9. `compliance_blocks`: `contact_e164`, `from_e164`, `reason`
  String(32), `body_excerpt` String(255), `exemption` String(32) nullable. Index
  `(org_id, contact_e164, created_at)`.
- **media_assets** (TenantScoped): `message_id` GUID FK→messages SET NULL nullable
  indexed, `direction` String(8), `storage_key` String(255) nullable,
  `content_type` String(127) nullable, `size_bytes` Integer nullable,
  `sha256` String(64) nullable, `source_url` Text nullable, `status` String(16)
  (`pending | stored | failed | too_large | unsupported | purged`),
  `fetch_attempts` Integer default 0, `next_attempt_at` tz nullable,
  `last_error` String(255) nullable, `expires_at` tz nullable.
  Index `(status, next_attempt_at)` for the fetcher.
- **message_templates** per DR-8 (lives in `models/messaging.py`).
- Columns: `messages.hold_until`, `contacts.timezone`.
- Data: re-seed system roles with the four new permission keys (0003 mechanism).

### 2. Gate (`compliance/gate.py`) — the phase's centerpiece

```python
async def check_outbound(session, org_id, draft, *, exemption: str | None = None) -> ComplianceVerdict
```
Order: opt-out (derived latest per DR-3) → internal DNC → quiet hours (DR-4, with the
24 h conversational carve-out and the recipient-tz resolution chain). Exemption
`"compliance_auto_reply"` short-circuits to allow (audited). Deny → write + commit the
`compliance_blocks` row, return `ComplianceVerdict(False, reason)`. Quiet →
`ComplianceVerdict(False, "quiet_hours", defer_until=not_before)`.
`on_inbound(session, org_id, message_id)`: load the message (scoped — org context is
already set by the caller), `classify_keyword(body)`, on hit: insert the ledger event
(IntegrityError on the `message_id` unique → someone processed it → return), then send
the auto-reply via `svc.send_message(..., exemption="compliance_auto_reply")` from the
receiving number. Auto-reply failure is logged, never raised into ingestion.

### 3. Send path (`services/messaging.py`)

`send_message` gains `media_ids` and `exemption` passthrough. Flow additions: verify +
load media assets (scoped, `status=="stored"`, ≤10, else 422); on verdict defer →
persist row (`status="queued"`, `hold_until`), link assets, commit, return — no carrier;
on allow → existing flow, with `OutboundMessage.media` = 72 h signed URLs, assets'
`message_id` stamped in the same commit as the row. `_dispatch_to_carrier` factored so
`release_held_messages` shares the accepted/rejected handling verbatim.

### 4. Media (`services/media.py`, `api/routes/media.py`, `storage/`)

Per DR-1/DR-2. Upload endpoint reads the whole body (≤3.75 MB enforced *before* store
write; oversize → 413 or 422 with code `media_too_large` via `ValidationFailedError`),
sha256s, stores, returns `{id, content_type, size_bytes, url}` (15 min signed).
Content endpoint: JWT+org path uses the scoped session; signature path loads the asset
by id `allow_unscoped` **with a justifying comment** (a signed URL is bearer
authorization for exactly one asset) and streams bytes with the stored content-type and
`Cache-Control: private, max-age=300`.

### 5. Quiet hours details worth pinning

"HH:MM" strings parsed once into `time` objects; the window is inclusive-start
exclusive-end in local wall time; `window_start < window_end` is validated (no overnight
windows — the columns name the ALLOWED window per DR-9, never "quiet start", precisely
so the classic inversion bug cannot be written; pin with a test that 21:30 is blocked
and 12:00 allowed under defaults). `not_before` for a blocked send = max over
candidate zones of (next local `window_start` as a UTC instant). DST correctness comes
from constructing `datetime` in the zone at that date, never from adding offsets.

### 6. Loopback media echo

`LoopbackCarrier._events_for` echoes `msg.media` into the inbound `InboundMessage.media`.
In the dev demo the fetcher then downloads from our own signed URLs over 127.0.0.1 —
the full pipeline against ourselves. In tests the fetcher is driven directly with a
MockTransport; no self-HTTP.

### 7. Frontend (scoped tight)

- Composer: attach button → `POST /media` → chips with thumbnail/name + remove; sends
  `media_ids`. Template picker: list from `/templates`, on pick call `render` with the
  thread's contact, insert body (+ template media), show `warnings`.
- `MessageBubble`: `media[]` → `image/*` as `<img>` (signed URL), else a typed file chip
  linking the URL. A held message renders its clock state ("scheduled — quiet hours,
  sends after {hold_until}").
- ThreadView: on open, `GET /compliance/consent/{contact_e164}`; `opted_out` → red
  banner + composer disabled (help-text explains START). A 422 `compliance_blocked`
  from send renders the reason inline.
- `CompliancePage` (admin-gated by a 403 probe like P2's AssigneeMenu pattern): quiet
  window (clamped inputs), auto-reply texts, DNC table (add/remove with reason), scrub
  textarea → per-number result list that **always displays "federal registry: not
  checked"** — the UI repeats the stub's honesty.
- Regenerate `openapi.json` + `types.gen.ts`; drift check already gates CI.

### 8. Explicitly NOT in P3 (scope fence — reject any of these in review)

- **No scheduler, no scheduled-send API, no campaigns, no pacing/warm-up, no CSV
  import** (P11 — `scrub` and defer are built *for* it, not *as* it). The DR-5 sweeper
  loop is the entire background machinery; no Redis, no worker processes.
- **No S3/MinIO backend** (P5 — `build_store` refuses `s3` loudly). No media
  transcoding, resizing, or format conversion; no group MMS; no vCard parsing.
- **No federal DNC subscription integration and no litigator-list API** — the stub only,
  un-mistakable per DR-6. No state two-party-consent data or recording-consent
  enforcement (P5; the ledger's `channel` column is P3's whole contribution — DR-7).
- No consent categories (marketing vs transactional) — one channel-level consent in v1,
  documented. No consent-ledger export/reporting UI (P13 audit surface).
- No 10DLC/TFV anything (P4). No link shortening/click tracking (P13). No RCS/WhatsApp.
- No AI classification of conversational refusals ("stop texting me" flags) — P10/P13;
  the manual opt-out endpoint is v1's answer.
- No per-message quiet-hours override flag on the send API (the carve-out + exemption
  are the only bypasses). No admin "clear opt-out" backdoor (DR-3's 409 stands).
- No new FastAPI middleware, no rate limiting. No changes to sticky-sender, inbox
  aggregate shape (beyond the optional media_count query), 2FA, or contacts CRUD
  semantics.

## Test Spec

All local commands from `backend/` in the venv, on this Windows machine, today:

```
python -m pytest -q          # SQLite; pg_only + live_carrier skipped — must be green
python -m ruff check .       # must be clean
```
Frontend — from `frontend/`, Node 22:
```
npm ci && npm run gen:api && git diff --exit-code openapi.json src/api/types.gen.ts
npm run typecheck && npm test -- --run && npm run build
```

Unit tests:
- [ ] `test_keywords.py::test_exact_match_families` → parametrized: every keyword in the
      three families matches with trims/case/trailing `. ! ,` (`" stop. "`, `"HELP!"`,
      `"Unsubscribe"` → hits with the right kind).
- [ ] `test_keywords.py::test_footer_false_positive_regression` — **THE INCIDENT
      REGRESSION**: `"Reply STOP to unsubscribe"`, `"please stop texting me"`,
      `"I want to stop"`, `"Yes I'm interested in the house"`, `"can you help"` → all
      `None`. This test is the codified memory of the STOP-footer DNC incident.
- [ ] `test_quiet_hours.py::test_npa_table_structural` → every zone in `NPA_TZ` loads in
      `zoneinfo`; spot-checks per DR-4 (incl. 602 Phoenix no-DST, 808 Honolulu).
- [ ] `test_quiet_hours.py::test_window_and_dst` → explicit `now` instants: 20:30
      America/New_York allowed and 21:30 blocked on BOTH 2026-01-15 and 2026-07-15;
      instants straddling 2026-03-08 spring-forward and 2026-11-01 fall-back evaluate
      correctly; blocked verdicts carry `not_before` == next 08:00 local as UTC.
- [ ] `test_quiet_hours.py::test_unknown_npa_fails_safe` → an unknown NPA is allowed
      only when inside 08:00–21:00 in ALL seven fallback zones; 18:00 ET (07:00 HST
      pre-8am Hawaii) → blocked. Contact.timezone set → it wins over the NPA.
- [ ] `test_object_store.py` → LocalFS put/get/exists/delete round-trip in `tmp_path`;
      key traversal (`../`) rejected; InMemory parity; `build_store` with backend `s3`
      → `ConfigurationError` naming P5.
- [ ] `test_templates.py::test_render` → tokens substitute from contact + attributes +
      org; unknown token → 422; empty known value → "" + warning listed.

Integration tests (httpx `AsyncClient`, SQLite, loopback/Fake carrier, frozen clock):
- [ ] `test_optout_engine.py::test_stop_suppresses_whole_pool` — **THE P3 GATE TEST**:
      org with numbers A and B; conversation on A; inbound `STOP` to A → ledger event
      (source `keyword`, `message_id` set); auto-reply confirmation sent FROM A
      (FakeCarrier captured, exemption audited); a send to that contact **from B** →
      422 `compliance_blocked`, zero message rows for it, zero carrier calls, one
      `compliance_blocks` row with reason `opted_out`.
- [ ] `test_optout_engine.py::test_stop_replay_is_single_shot` → replay the identical
      STOP webhook 3× → one ledger event, one auto-reply (message-count proof).
- [ ] `test_optout_engine.py::test_start_reopts_and_help_replies` → START after STOP →
      sends allowed again, opt-in event appended (ledger now 2 rows, nothing mutated);
      HELP from an opted-out contact still gets the help auto-reply (exemption).
- [ ] `test_optout_engine.py::test_manual_optin_cannot_override_keyword_stop` → keyword
      STOP then `POST /compliance/optin` → 409; after a *manual* opt-out instead → 200.
- [ ] `test_optout_engine.py::test_ledger_is_append_only_and_voice_invisible` → no
      UPDATE/DELETE endpoints exist (404/405); a hand-inserted `channel="voice"` opt-out
      does NOT block an SMS send.
- [ ] `test_dnc.py::test_dnc_blocks_and_audits` → add DNC entry → send → 422 reason
      `dnc`; remove → send passes; both edits appended `dnc_add`/`dnc_remove` ledger
      events; org B cannot see org A's entries (tenancy).
- [ ] `test_dnc.py::test_scrub_report_honest` → scrub of [clean, opted-out, DNC'd] →
      per-number verdicts correct AND every result has `federal_checked == False` and
      reason `"federal_dnc:unchecked"`; `provider_statuses()` contains the disabled
      `federal_dnc` entry.
- [ ] `test_held_messages.py::test_defer_and_release` → clock frozen inside recipient
      quiet hours (explicit patch) → send → 201 `status=="queued"` + `hold_until`, zero
      carrier calls; advance the frozen clock past `hold_until` → `release_held_messages`
      → dispatched via carrier, status `accepted`; re-running release is a no-op.
- [ ] `test_held_messages.py::test_optout_during_hold_kills_send` → hold a send; STOP
      arrives; release → status `rejected`, `error_code=="opted_out_while_held"`, zero
      carrier calls. (This IS the in-flight answer, and P11's dispatch-time-gate contract.)
- [ ] `test_held_messages.py::test_conversational_carveout` → contact with an inbound
      in the last 24 h → a reply during quiet hours sends immediately; a contact with
      no recent inbound → held.
- [ ] `test_media_pipeline.py::test_upload_validate_send` → multipart upload stores +
      returns signed URL; >3.75 MB → 422 `media_too_large`; disallowed type → 422;
      send with `media_ids` passes signed URLs to the carrier (Fake captures them) and
      stamps `message_id` on the assets; another org's media id → 422.
- [ ] `test_media_pipeline.py::test_inbound_fetch_outside_webhook` → inbound MMS webhook
      (fixture with `media[]`) → 200 with the MockTransport call-log EMPTY (zero HTTP in
      the request path), `media_assets` rows `pending`; replay → no duplicate assets;
      then `fetch_pending_media` with MockTransport → `stored`, bytes in the store,
      sha256/size recorded; a 404-ing URL backs off (`next_attempt_at`, attempts) and
      after 6 attempts is `failed`; Bandwidth-host URL gets Basic auth attached,
      foreign host does not.
- [ ] `test_media_pipeline.py::test_signed_url_contract` → valid sig serves bytes with
      stored content-type; expired → 403; tampered → 403; JWT of the owning org works;
      JWT of another org → 404.
- [ ] `test_media_pipeline.py::test_retention_purge` → retention configured → asset
      past `expires_at` purged from store, row status `purged`; retention 0 → untouched.
- [ ] `test_templates.py::test_crud_render_permissions` → agent can list/render, cannot
      create (403); render via API round-trips contact attributes; tenancy on templates.
- [ ] **Frozen-file proof:** `test_compliance_seam.py` passes UNMODIFIED (run it by
      name in review: `python -m pytest tests/test_compliance_seam.py -q`).
- [ ] (`pg_only`) opt-out gate + held-release + media dedupe re-run on Postgres
      (constraint parity for the `message_id` unique and the asset replay path).

Frontend tests (vitest, jsdom, stubbed ApiClient):
- [ ] `composer.media.test.tsx` → attach flow calls upload, chips render, send carries
      `media_ids`; oversize upload error surfaces.
- [ ] `threadview.optout.test.tsx` → consent `opted_out` renders the banner and disables
      the composer; `compliance_blocked` 422 renders the reason.
- [ ] `bubble.media.test.tsx` → image media renders `<img>` with the signed URL; a held
      message shows the quiet-hours chip with `hold_until`.
- [ ] `compliance.page.test.tsx` → scrub result list always shows "federal registry:
      not checked"; DNC add/remove calls the right endpoints.

Manual verification:
- [ ] P3a demo (loopback + `npm run dev`): perform the three-part P3a gate from the Goal
      section end-to-end in the browser, including the footer-false-positive check and
      watching a held message release after temporarily shrinking the sweeper interval.
- [ ] CI: all five jobs green; `test-postgres` + `frontend` remain the merge gates.
- [ ] P3b (operator, post-R1): live handset STOP→pool-block, quiet-hours defer, MMS
      round-trip with media rendered; live MMS webhook payloads recorded into
      `tests/fixtures/bandwidth/recorded/` and diffed; PROGRESS updated.

Pass criteria — **P3a:** every check above green locally (3.10 + SQLite / Node 22) and in
CI; ruff clean; `test_compliance_seam.py` untouched and green; demo performed. Commit
`feat(phase-3a): compliance core (consent ledger, STOP/START/HELP, quiet hours, DNC), MMS
pipeline, templates`. **P3b:** the live gate demonstrated. Commit
`feat(phase-3b): live MMS + STOP verified on Bandwidth`. P4 (and P11 design work) may
start on P3a sign-off.

## Deploy

**P3a: no.** Nothing new on the VPS.
**P3b: yes — operator-supervised, with/after P1b+P2b.** `alembic upgrade head` runs
`0004` (additive). `.env` on the box gains the DR-9 settings (operator); media root
`/opt/csaas/var/media` on the box's disk (LocalFS backend is the production store until
P5 brings S3/R2 for recordings) — confirm free-disk headroom in pre-flight. nginx: the
existing `/api/` proxy already covers the media content endpoint; raise
`client_max_body_size` to 4m for the upload path in the site file. All P0 VPS
constraints restated: everything under `/opt/csaas`, api on 127.0.0.1:8080 only, no
global installs, self-contained site file, loopback flag absent/false in prod (boot
refuses it anyway).
