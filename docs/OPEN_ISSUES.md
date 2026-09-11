# OPEN ISSUES — running ledger for the P11–P14 push

> Created 2026-08-29. Every issue found while executing P11–P14 that is NOT solved
> in-phase gets a row here, to be resolved in a dedicated cleanup pass at the end.
> When one is fixed, mark it ✅ with the commit. Do not silently drop rows.

## External inputs (user-only — cannot be coded around)

| # | Issue | Blocks | Unblock action |
|---|---|---|---|
| E1 | **B1: no messaging-capable carrier.** Telnyx keys absent from `/opt/csaas/.env`. | P1b, P4 registration, P10 live SMS turn, P11 live campaign gate | User pastes `TELNYX_API_KEY` + `TELNYX_MESSAGING_PROFILE_ID`, restarts api. 10DLC brand/campaign in Telnyx portal. |
| E2 | **B2: no SIP trunk points at the box.** | P5–P9 runtime gates, P11 dialer live gate, P12 live gate | Bandwidth portal: voice app → inbound SIP peer `144.126.152.175:5060`, assign `+19404060664`. Port is open and listening (verified 2026-08-29). |
| E3 | **B4: no AI provider keys in production.** `ANTHROPIC_API_KEY` / `DEEPGRAM_API_KEY` / `ELEVENLABS_API_KEY` empty. | P8/P9 voice agent, P10 SMS agent LLM turn | User pastes keys into `/opt/csaas/.env`, restarts api. |

## Infrastructure gaps (code/config work, deferred deliberately)

| # | Issue | Found | Notes |
|---|---|---|---|
| I1 | **nginx has no wss proxy for LiveKit 7880** — browser softphone cannot connect. | 2026-08-29 B3 bring-up | Additive `location` block on the csaas nginx site + `nginx -t` + reload. Deferred because nginx is shared with other tenants; needs explicit go-ahead for the reload. |
| I2 | **No agents-worker service on the VPS.** `agents/` code is shipped but has no venv or systemd/compose unit. | 2026-08-29 | Needs Python 3.11+ venv on the box + a unit. Pointless before E2/E3. |
| I3 | **`deploy/livekit/README.md` and `sip.yaml` comments document a TELNYX trunk** — voice is Bandwidth now (D-split at P3). | 2026-08-29 | Doc fix; step 5 of the README must not be followed as written. |

## Code issues (carried from earlier phases)

| # | Issue | Found | Notes |
|---|---|---|---|
| C1 | ~~`test_softphone_token_cross_org_room_is_404` flake.~~ ✅ **ROOT-CAUSED + FIXED 2026-08-29 (P14 cycle):** org A's background dial task committed concurrently with org B's login on SQLite's StaticPool single connection, corrupting the login SELECT's cursor — exactly the auth-signature failure recorded. Became ~50% reproducible in isolation this session; a `wait_for_pending_dial_tasks()` drain closes the window (4/4 stable). Watch one more week of CI before deleting this row. | pre-2026-08-29 → fixed | |
| C2 | **Local dev SQLite runs the app with FKs unenforced** (pragma only set on the test engine). Prod is Postgres — dev-fidelity gap only. | P2a | |
| C3 | **R2 unmeasured:** VPS ↔ Bandwidth media PoP may be long-haul → TCP dead-air risk. Measurement scripts exist (`measure/`), need a live trunk. | planning | Run at P7 gate time (needs E2). |
| C4 | **R3 unmeasured:** Deepgram Nova-3 real WER on 8 kHz caller audio. | planning | P8 bake-off (needs E2+E3). |

## Discovered during P11–P14 (append below as found)

| # | Issue | Found | Notes |
|---|---|---|---|
| D1 | `pacing.predictive_coefficient` floor=0.25 overrides the 3% abandon target when observed abandon > 12%, and `parallel_lines=1` predictive campaigns can never throttle below 1 line — docstring overclaims a guarantee. | P11 Opus review | Tighten floor semantics or document honestly. |
| D2 | Predictive denominator counts compliance-blocked rows (never dialed) as "placed", understating abandon rate. | P11 Opus review | Exclude disposition="blocked" from placed. |
| D3 | `DIAL_STATUSES` lacks `blocked`; compliance refusals ship as `failed` + disposition="blocked". | P11 Opus review | Add `blocked` in the next migration touching outbound. |
| D4 | Uploaded list source blob (`org/{org}/imports/{list}/source`) is never deleted and has no retention. | P11 Opus review | Delete after import completes, or add to media purge sweep. |
| D5 | `SEND_TERMINAL` includes `deferred`: a campaign reads "completed" while gate-held sends are still awaiting sweeper release. | P11 Opus review | Progress endpoint should surface held count; consider completing only when releases resolve. |
| D6 | Campaign pause is honored between ticks only — a pause mid-tick still sends the remaining batch (max `OUTBOUND_TICK_BATCH`). | P11 Opus review | Re-check status inside the send loop if this matters at larger batch sizes. |
| D7 | Dial tick batch budget charges deferred/compliance-blocked rows though no dial was placed. | P11 Opus review | Cosmetic throughput loss. |
| D8 | No RBAC deny tests for `/api/v1/outbound/*` (generic deny path covered elsewhere). | P11 Opus review | Coverage hygiene. |
| D9 | `allow_unscoped` uses in outbound.py/dialer.py lack the inline justification comment style db/base.py mandates. | P11 Opus review | Move rationale inline. |
| D10 | `resolve_or_create_contact`'s IntegrityError path rolls back the whole session — inside `run_import`'s 200-row batches a genuine phone race discards the current batch. | P11 implementer | Narrow to a nested savepoint. |
| D11 | List import re-checks DNC/opt-out per row (two queries/row) — fine at 500 rows, quadratic pain at 50k. | P11 implementer | Batch the scrub queries. |
| D12 | Campaign throughput is bounded at ~1 send/number/tick (one frozen `now` per tick) — high-volume orgs need a short sweeper interval, not bigger batches. | P11 implementer | Document in ops runbook (P14). |
| D13 | Voice-agent (LiveKit worker) token usage is not reported to the backend — `ai_tokens` metering covers SMS turns only. | P13 planning | Extend the worker transcript seam to carry usage. |
| D15 | **Whisper has no server-side enforcement** — verified live: LiveKit RoomService has no SetSubscriptionPermissions twirp (404). Real whisper = the supervisor's CLIENT sets track subscription permissions at publish time (SFU-enforced) — needs the softphone client (blocked on I1 + B2). Whisper endpoint ships as FeatureUnavailableError. | P12 Opus review | Implement in the softphone client when it lands. |
| D16 | Room-path queue overflow (voicemail/hangup) is a state change only — cannot record a greeting or tear down the room without voice_plane work + an audio-publishing participant (see I5). | P12 Opus review | Pairs with the agents-worker deployment (I2). |
| D17 | `activate_flow` does not re-point bound numbers and `resolve_inbound_flow` doesn't require status=="active" — a number keeps running its pinned (possibly archived) version until re-bound. Consistent with pinning philosophy; recorded as intended-but-surprising. | P12 Opus review | Documented behavior; revisit if operators trip on it. |
| D18 | Business hours: overnight windows ("22:00"–"02:00") evaluate closed; schedule weekday keys are not validated (typo = silently closed). | P12 Opus review | Validate keys + support overnight windows. |
| D19 | `services/media.py` fetch loop has the same multi-org single-commit autoflush hazard that broke routing_tick (B1) — pre-existing, latent. | P12 Opus review | Apply the per-row-commit pattern. |
| D20 | `routing_tick` has no batch limit. | P12 Opus review | Fine at current scale. |
| D21 | Webhook SSRF guard has an honest DNS-rebinding TOCTOU: the private-IP check runs on our resolution; httpx re-resolves at request time. Documented in webhooks_out.py. | P13 Opus review | A pinned-IP transport would close it; not v1. |
| D22 | `call_scores` is write-only (no read endpoint by DR-8 scope) and a `disabled` score is permanent even after keys arrive; scoring has no lookback window (first ticks after enabling spend LLM on all history). `summary=="retry_exhausted"` sentinel must map to None in any future serializer. | P13 Opus review | Bundle into the scores read endpoint when built. |
| D23 | DR-11's `GET /api/v1/openapi-public.json` deferred by Fable ruling — docs nicety, not gate-relevant. | P13 | Small filter over app.openapi(). |
| D24 | `role.changed` audit action unwired — no member-role-update route exists anywhere yet. | P13 Opus review | Wire when the route exists. |
| D25 | Transcript search: tsvector-path stemmed hits may return a call with no segment flagged `matched` (substring flagging). Campaign "progress" in analytics is a status snapshot, not a daily series. | P13 Opus review | Cosmetic. |
| D26 | **No rate limiting on /auth/login or TOTP verify** (security review S1, MEDIUM) — unthrottled 6-digit TOTP guessing is the sharp edge. | P14 security review | In-process per-identifier limiter; small, no new deps. First item of the cleanup pass. |
| D27 | Events WS JWT travels in a query param (proxy-log exposure; verified same decoder+membership as HTTP). | P14 security review | Short-lived WS ticket endpoint later; runbook notes the nginx logging expectation. |
| D28 | **Voice does not walk the failover plan** (DR-2 amended; SMS failover complete). Blocked on two real gaps found in review: `CreateCallResult` has no error taxonomy (breaker can't be fed) and the dial path builds no `RoutePlan`. Recipe when B2 makes voice exercisable: `routes/calls.py::_resolve_outbound` returns an ordered candidate list (explicit from/carrier honored-or-refused as single-element; else active voice-capable candidates, healthy breakers first, same-carrier before cross-carrier, cross only when org policy allows); `services/calls.py::create_outbound_call` accepts the list and loops — accepted breaks, rejection appends a CallLeg reason="failover" and continues; extend `CreateCallResult` with a classified CarrierError so the breaker can record voice failures. | P14 Opus review | Land with B2. |
| D29 | Spam-class error-code lists in reputation.py (Bandwidth 4750-4754/4770-4775, Telnyx 40002/40003/40015/40017/40020/40322) sourced from public docs, not observed traffic. | P14 | Validate against real DLRs once B1 is live. |
| D14 | Voice campaign.completed / voicemail hold events: `campaign.completed` outbox hook lands with the P11 fix round; `voicemail.created` must be wired inside P12's services/voicemail.py. | P13 planning | Verify both hooks exist before P13 webhook tests rely on them. |

## Discovered during P15 (Opus review — approved non-blocking, land in P16+)
| ID | Issue | Found | Recipe |
|---|---|---|---|
| D30 | Four P15 guards lack failing-test coverage if reverted: `get_recording` (calls.py), `list_voicemails`/`mark-read` + `monitor`/`whisper`/`barge` (flows.py), and the `_CALL_ID_EVENTS`/`_THREAD_ID_EVENTS` DB-lookup branch of `_event_visible` (only the `call.ring` branch is unit-tested). Correct by inspection + Opus line-by-line verify. | P15 Opus review | Add regression tests before these routes are next touched. |
| D31 | `_resolve_event_e164` opens a session per event per connected WS socket; `call.status` fires per leg transition. | P15 Opus review | P16: stamp `our_e164` into event payloads at publish time (like queue `call.ring` now does), or cache `call_id → our_e164` per connection. |
| D32 | WS TTL access re-resolve: a DB blip kills `_forward_events` (fail-closed, client reconnects). Acceptable; could keep previous access + log instead. | P15 Opus review | Wrap TTL re-resolve in try/except in P16 softphone work. |
| D33 | Deliberate P15 scope lines (decided, not oversights): analytics aggregates are org-wide counts (no content); `campaigns:manage` holders may set campaign `from_numbers` without inbox grants (admin-tier concern — make explicit in P16); `/inbox/threads` P15 filter is a post-filter so a page can return short (push into `inbox_svc.list_inbox` in P16); API-key callers bypass the inbox tier (P13 DR-3). | P15 review | Revisit each in P16 UI phase. |

## Discovered during P17 (Opus review — approved non-blocking, land in P18)
| ID | Issue | Found | Recipe |
|---|---|---|---|
| D34 | Org registry cache: a version bump leaves the previous `(org, old_version)` entry and its db-owned adapter open until 256-entry LRU pressure (bounded, not unbounded). | P17 Opus review | In `_cache_org_registry`, evict every existing key with `k[0] == org_id` (awaiting adapter close) before inserting. |
| D35 | Voice webhooks have NO DB-account fallback — a DB-only provider org's inbound CALLS still resolve against the env registry (messaging fallback exists). P17's "no .env edits" goal holds for SMS only. | P17 Opus review | Mirror `_db_account_carrier_verifying` with `verify_voice_webhook` in `_handle_voice_webhook`; land with P18 number purchasing. |
| D36 | Carrier catalog (`/routing/catalog`) mixes truths: registry part is per-org via the proxy, but `settings.provider_statuses()`/`carrier_requirements()` are env-derived, so an org with DB-only Telnyx may still see "Needs credentials" in the health section. | P17 review | Make provider_statuses/requirements account-aware in P18. |

## Discovered during P18 (Opus review)
| ID | Issue | Found | Recipe |
|---|---|---|---|
| D37 | Bandwidth Numbers API (dashboard.bandwidth.com) is called with Basic auth (api_username/api_password) while messaging/voice use OAuth2 client credentials — NOT verified against the live account (trial). 401/403 now maps to a clear ValidationFailedError. | P18 Opus review | Verify one real search once the account is upgraded; if the dashboard API user differs, add a separate credential pair. |
| D38 | Bandwidth XML size cap runs on `resp.text` (after httpx buffered the body) — bounds parse cost, not memory. | P18 Opus review | Stream + cap `Content-Length`/bytes read if it ever matters. |

## Discovered during P22 (Opus verify, 2026-09-10 — approved non-blocking)

| ID | Issue | Found | Recipe |
|---|---|---|---|
| D39 | List import's owner summary (`unknown_owner_emails`, `assigned`) is computed but never persisted or shown; only logged. | P22 Opus verify N2 | P27 (Contacts pro) adds an `import_summary` JSON column on `contact_lists` (migration 0029) and shows it on the Lists tab. |
| D40 | `/agent/contact/{e164}` returns a blank profile (200, name "") for a contact outside the inbox's department, but `last_messages` still come back in full. Decided: intended — thread data is the worker's own; profile fields follow the policy. | P22 Opus verify N3 | none |
| D41 | An owner demoting themselves as the last owner gets 409 "last owner" before the 403 "own role" check. | P22 Opus verify N5 | cosmetic; leave |
| D42 | **`sa.select(sa.func.count()).select_from(Model)` is NOT tenant-scoped**: the `with_loader_criteria` guard attaches to mapped entities, and a bare count has none, so no org filter is added and the guard does not raise. Found live in P20's `member_count` (counted every org). Existing instances: dialer.py (explicit org_id, safe), outbound.py:372 and sms_agent.py:664 (keyed on globally-unique columns, no live leak). | P20 Opus supervisor W1 | Always count a mapped column (`count(Model.id)`). Add a ruff/AST lint that bans `func.count()` + `select_from` without a column (P36 trust phase, or earlier if cheap). |
| D43 | **Campaign sends bypass routing entirely**: `services/outbound.py` calls `send_message(plan=None)`, so campaigns never consult `rank_routes`, get no `route_reason`, and a number in a spam-class breach is NOT excluded for campaign traffic (the P21 spec rule is unreachable in production). Pre-existing; `outbound.py` was outside P21's allowed files. | P21 Opus verify N1 | P28 (messaging completeness): build the plan for campaign sends through `plan_route(is_campaign=True)` and record the sentence; keep the pinned test `test_campaign_send_does_not_exclude_a_breached_number_in_production` and flip it. |
| D44 | Assistant simulator (`POST /agent/profiles/{id}/simulate`) only supports OpenAI and Anthropic language models; DeepSeek/Groq/Google assistants get a plain 422 (`services/llm_client.py` was outside P23a's allowed files). | P23a Opus supervisor | P23b: extend llm_client with the three OpenAI-compatible endpoints (DeepSeek, Groq) and Google; flip the 422 test. |
| D45 | Knowledge URL ingestion: no DNS-rebinding guard and no byte cap on the streamed download (same TOCTOU class as D21). | P23a Opus supervisor | P36 trust phase (or P23b if cheap): reuse the outbound-webhook SSRF guard + cap at 10 MB. |
| D46 | **Worker token is a master key once `AI_PER_ORG_KEYS=1`**: `verify_worker_token` accepts one global JWT (LiveKit secret) with no org/call binding, so any holder can walk call ids and pull every org's decrypted BYOK keys from `/agent/config/{call_id}`. Flag ships OFF. | P23a Opus verify | P23b (before the flag is ever enabled): mint short-TTL per-call worker tokens bound to call_id (+ org), reject unbound tokens on the config endpoint. |
| D47 | BYOK config resolution takes the OLDEST active account per kind and discards it if the profile names a different provider; a second active account (e.g. anthropic next to openai) is silently "missing". | P23a Opus verify | P23b: resolve by (kind, provider) in `active_account_for`. |
| D48 | Knowledge document create writes no audit row (every provider route does). | P23a Opus verify | P23b: one `audit_svc.record(...)` in the kb documents POST/DELETE. |
| D49 | `deploy/deploy.sh` prints "command substitution: line 142: syntax error near unexpected token `||`" at the livekit/sip config render step (a `$(...)` wrapping a heredoc'd ssh with `|| die`); the step still completes because the rendered files already exist, but a fresh box would not get them rendered. | P26 deploy 2026-09-11 | Fable: restructure line 142 so the `|| die` sits outside the substitution; verify by deleting the rendered yaml on a staging path and re-running. |
| D50 | The voice worker (external repo) must read `metadata.worker_token` (per-call bound token, D46) before `AI_PER_ORG_KEYS=1` is ever set; `/agent/config` now rejects the global token. | P23b Opus supervisor | Worker repo change; track in its handoff. |

## Discovered during P24/P27 (Opus review, 2026-09-10 — fixed in-phase, recorded for the log)

| ID | Issue | Found | Recipe |
|---|---|---|---|
| D51 | `GET /contacts/{id}/export-my-data` requires only `contacts:read`, so any agent who can read a contact can download that person's full PII bundle including consent/DNC history — no `compliance:manage` gate, unlike `POST /{id}/erase`. Per the P27 handoff spec (not a bug), but worth a deliberate decision. | P27 Opus supervisor | Tier-1 decision needed: keep as-is (export is read-adjacent) or require a compliance permission. Not blocking; revisit if it becomes a real complaint. |
| D52 | P24's credit sweeper (`credits_tick`) called `check_balance_warnings(session, org_id, settings=...)` against a signature that takes an `Org` row and no `settings` — every tick raised `TypeError`, swallowed by the per-org broad `except`. Silently dead: no stale-reserve release, no low-balance warnings, no campaign auto-pause, since the sweeper was first drafted. | P24 Opus supervisor, fixed same phase | ✅ Fixed in commit 578bf9e — signature corrected, covered by a new sweeper test. |
| D53 | P24's `release_for_call` treated `credits.release()`'s idempotent-replay return (the existing row, never `None`) as "nothing to release," so every retried call-release falsely logged that credits were returned even when they weren't (first call already released them correctly; only the false-positive log on replay was wrong). | P24 Opus supervisor, fixed same phase | ✅ Fixed in commit 578bf9e — truthiness check replaced with an explicit already-released check, covered by a new test. |
| D54 | Platform ops billing UI (P24) has no way to browse/search orgs — `backend/app/api/routes/platform.py` only exposes `GET/PATCH /platform/billing/orgs/{org_id}` (by id), no list/search endpoint. Shipped with a manual "enter org ID" input instead. | P24 frontend integrator, 2026-09-10 | Add a paginated `GET /platform/billing/orgs?q=` search endpoint (name/email/id) when an operator actually needs to browse rather than paste an id; low priority internal-tooling polish. |

## Discovered during P25 (Opus supervisor, 2026-09-10 — nine findings fixed in-phase before staging; four recorded here)

| ID | Issue | Found | Recipe |
|---|---|---|---|
| D55 | No pruning job for `login_events` — every failed login writes and commits a row (rate-limited but not retained-limited); an attacker rotating emails writes one row per attempt, and `blocked_ip` events from allowlist rejections have the same growth pattern. | P25 Opus supervisor | Add a sweeper (~180-day retention) before SSO/2FA policy is exposed to the internet at volume. |
| D56 | No 2FA break-glass: after fixing S3 (enabling `require_2fa` now requires the caller already hold a second factor, closing a permanent-lockout hole), an org whose only 2FA-holding admin loses their device has no way to disable the policy. Matches this repo's existing gap on the other product (`LOGIN_MFA_ENFORCE=0`) — no equivalent exists here yet. | P25 Opus supervisor | Add an operator/Fable-level break-glass path, deliberately outside the normal admin UI. |
| D57 | `issue_state` (SSO `/start`) does not catch Redis errors the way `consume_state` (the callback) does — a Redis outage surfaces as a 500 instead of a clean 503. | P25 Opus supervisor | Low priority; wrap `issue_state` the same way. |
| D58 | The SSO callback returns the minted token as JSON (`{access_token, token_type, org_id}`), not a redirect — deliberate (a token in a query string lands in logs/Referer/browser history), but the frontend phase must complete the flow by reading that JSON rather than expecting a redirect. | P25 Opus supervisor | P25 frontend (not yet started): build the SSO landing page around the JSON response; also wire the `two_factor_required_for_actor` and `ip_allowlist_would_lock_you_out` 422 error codes to inline Security-tab messages. |

Two deliberate deviations from the P25 handoff, both reviewed and accepted: OIDC is built on `PyJWT` + `httpx` (already runtime deps) instead of the handoff-approved `authlib`, since the standing no-new-dependencies rule takes precedence and authlib was never actually installed by the prior attempt (its vendored `pylibs/` hack was deleted, not shipped); the 60s revocation cache is in-process by default and upgrades to Redis automatically when `REDIS_URL` is set, rather than requiring Redis outright — with more than one API worker and no Redis, a revocation is instant on the worker that performed it and up to 60s late on the others, which matches the handoff's stated tolerance either way.

## Discovered during a live production test call, 2026-09-11 (fixed same day)

| ID | Issue | Found | Recipe |
|---|---|---|---|
| D60 | `voice_plane/livekit_api.py::admin_token()` never carried a `sip` grant — LiveKit's SIP twirp service (`CreateSIPParticipant`, the actual dial-out step) authorizes off that grant independently of `video`/roomAdmin, which RoomService honors. Every "via: room" outbound call created its LiveKit room successfully (200) then 401'd on the SIP dial-out, surfacing to the caller as "unauthenticated / permissions denied" with no ring at all. | Live test call, 2026-09-11 | ✅ Fixed in commit 6ea0866 — `sip_grants={"admin": True, "call": True}` added to the admin token. Deployed same day. |
| D61 | `LiveKitApi` minted every admin token with `room: "*"`, but LiveKit matches `roomAdmin` against the token's EXACT room name - there is no wildcard. Every room-scoped RPC 401'd: RemoveParticipant (seen live on hangup 2026-09-11), and by the same rule ListParticipants + UpdateSubscriptions (P29 coaching), CreateDispatch (P23b AI assistant - it would never have been dispatched), TransferSIPParticipant (warm transfer). | Live test call, 2026-09-11 | ✅ Fixed - `_twirp(..., room=)` mints the admin token for the exact room on each room-scoped call; regression test in `tests/test_voice_plane.py`. Egress RPCs still lack a `roomRecord` grant (unwired - fix when egress capture is wired). |
| D62 | Browser softphone: `joinRoom` set the room current and showed the call only AFTER `room.connect()` resolved. A slow ICE connect hid the call UI ~15s, a re-click placed a second real call, and the stale room's late Disconnected wiped the live call's state and all remote `<audio>` - no audio either direction, while the SIP/Telnyx leg carried clean two-way RTP. | Live test call, 2026-09-11 | ✅ Fixed in the softphone commit - room current before connect, call shown immediately, `dial()` refuses while a call is in flight, superseded connects are torn down; 5 regression tests. |
| D63 | `tests/test_p24_billing_api.py::test_usage_groups_by_metric_and_totals_the_price` fails ~1 in N FULL-suite runs but passes alone and as its whole file in either order - an inter-file order/state dependency. **Correction:** commit 42deb0f's message says "full suite green"; that run actually showed 1 failed / 1738 passed (the "exit 0" read was `tail`'s exit code in a pipe, not pytest's). The failure is this flake, unrelated to D61. | Full-suite run, 2026-09-11 | Open - find the shared state (likely a module-level cache or clock) the test inherits from an earlier file. Until then, re-run it alone before treating a full-suite failure as real. |

## Discovered during P29 frontend (Opus supervisor + Fable, 2026-09-11)

| ID | Issue | Found | Recipe |
|---|---|---|---|
| D64 | Agents (calls:read, no settings:read) could not pick a call result - the catalogue only lived behind `GET /orgs/current/calling` (settings:read). | P29 frontend supervisor | ✅ Fixed - `GET /api/v1/calls/dispositions` (calls:read), declared before `/calls/{call_id}`; the picker reads it. Regression test in `test_p29_voice_completeness.py`. |
| D65 | The timeline payload carries no `calls.disposition`, so the inbox call card fetches `GET /calls?contact_e164=…&limit=200` once per contact to show a saved result; calls older than the newest 200 show no picker. | P29 frontend supervisor | Add `disposition`/`disposition_note` to the timeline call event; drop the extra fetch. |
| D66 | No monitor / whisper / barge UI exists (hooks defined, unused). A whisper client must set track subscription permissions on the SUPERVISOR's side before publishing, or coaching audio can reach the customer. | P29 frontend supervisor | Build the supervisor coaching panel as its own slice; never ship a whisper button without supervisor-side subscription permissions. |
| D67 | P29 frontend removed the Calls page per-leg "Legs" table (raw provider hangup codes) under the phase's no-provider-codes / simplicity rule. | P29 frontend supervisor | Restore behind a platform-ops-only disclosure if support staff need raw leg data. |
| D68 | `frontend/src/api/types.gen.ts` / `openapi.json` are many phases stale (~9k-line diff); P29 shapes are hand-typed in `api/calls.ts`. | P29 frontend supervisor | Regenerate the OpenAPI types as a standalone chore. |
