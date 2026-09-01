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
