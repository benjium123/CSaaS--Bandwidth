# Full-system bug review — 2026-09-03 (Fable triage of 8 Opus area reviews)

Legend: FIX = this round · P20 = folded into the simplicity phase · ASK = needs a user
decision/approval · ACCEPT = documented, no change.

## Area 8 — Infra / config / deploy / migrations
| # | Sev | Item | Triage | Owner |
|---|---|---|---|---|
| 8.1 | HIGH | No volume for media/recordings/voicemail → wiped on every deploy | FIX (compose volume `csaas_media:/app/var/media`; note past media already lost) | infra |
| 8.2 | HIGH | `client_max_body_size 256k` server-wide breaks MMS upload + list import | FIX in repo template; live nginx edit = ASK (shared config) | infra |
| 8.3 | HIGH | No rate limiting on login/TOTP (D26) | FIX app-level in-process limiter (login + totp), nginx limit_req in template (live = ASK) | backend |
| 8.4 | HIGH | Migrations run after `up -d --build` | FIX deploy.sh: `run --rm api alembic upgrade head` before starting api | infra |
| 8.5 | HIGH | Frontend built from local tree (unreproducible) | FIX deploy.sh: build from `git archive HEAD frontend` export; abort on dirty frontend | infra |
| 8.6 | MED | No log rotation | FIX compose logging opts all services | infra |
| 8.7 | MED | Redis passwordless on shared host | FIX compose requirepass + maxmemory; needs `.env` REDIS_URL/password → ASK before applying | infra |
| 8.8 | MED | tar overlay never deletes stale files | FIX deploy.sh rsync --delete into /opt/csaas (keep .env, backups, var) | infra |
| 8.9 | MED | Deploy never applies livekit compose | FIX deploy.sh include second -f | infra |
| 8.10 | MED | No security headers | FIX template; live = ASK | infra |
| 8.11 | MED | credentials_master_key no prod guard | FIX config.py | backend |
| 8.12 | MED | public_web_url no prod guard | FIX config.py | backend |
| 8.13 | MED | .env.example drift (120 inert, 12 missing) | FIX docs (mark inert; add missing) | docs |
| 8.14 | MED | No offsite backup | ASK destination (S3/rclone target) | user |
| 8.15 | LOW/MED | SIP 5060 open to world | ASK (ufw to Telnyx signaling ranges) | user/infra |
| 8.16 | LOW/MED | livekit 500 UDP ports via docker-proxy | P20-infra window (host networking) | later |
| 8.17 | LOW/MED | uvicorn without --proxy-headers | FIX Dockerfile | infra |
| 8.18 | LOW/MED | Sweeper assumes 1 worker, nothing enforces | FIX pg_try_advisory_lock in sweeper + CMD comment | backend |
| 8.19 | LOW | healthz no retry | FIX deploy.sh loop | infra |
| 8.20 | LOW | redis no healthcheck | FIX compose | infra |
| 8.21 | LOW | CRON_TZ | FIX runbook | docs |
| 8.22 | LOW | .env perms unchecked | FIX deploy.sh preflight | infra |
| 8.23 | LOW | no lockfile | FIX: generate requirements.lock from the built image (`pip freeze`) and install from it | infra |
| 8.24 | LOW | backup.sh running-guard | FIX script | infra |
| 8.25 | LOW | double build in create_app/lifespan | FIX main.py reuse | backend |

## Area 5 — Inbox / conversations / contacts / access
| # | Sev | Item | Triage |
|---|---|---|---|
| 5.1 | HIGH | `GET /messages` without thread_id returns every message in the org (no access filter) | FIX: resolve access unconditionally; filter by thread.our_e164 ∈ visible |
| 5.2 | HIGH | `POST /softphone/token` mints a room token with no inbox check (join any inbox's live call) | FIX: `_access_or_404(require_use=True)` after `_call_for_room` |
| 5.3 | HIGH | `GET /search/transcripts` returns transcript content across all inboxes | FIX: filter Call.our_e164 ∈ visible unless admin |
| 5.4 | MED | Viewer can mark_read; last_read_at is per-thread (zeroes everyone's unread) | FIX now: mark_read requires can_use; per-user read state → P20 backlog |
| 5.5 | MED | `PUT /contacts/{id}/tags` doesn't validate tag ids (cross-org ref / 500) | FIX like set_labels |
| 5.6 | MED | company_id never validated on contact create/patch | FIX 422 |
| 5.7 | MED | Removing a phone leaves threads linked to the old contact | FIX unlink on delete |
| 5.8 | MED | `GET /contacts` N+1 phones | FIX batch |
| 5.9 | MED | LIKE wildcards unescaped in q (4 sites) | FIX escape + ESCAPE clause |
| 5.10 | MED | Keyset pagination drops NULL last_message_at threads / early terminate | FIX coalesce(last_message_at, created_at) in order + cursor |
| 5.11 | MED | Call-only conversation can never be marked read | FIX: read route accepts (our_e164, contact_e164) and upserts thread |
| 5.12 | LOW | assigned=me with API key returns unassigned | FIX 422 |
| 5.13 | LOW | render_template leaks contact PII under templates:read only | FIX also require contacts:read |
| 5.14 | LOW | No optimistic concurrency on thread claim | FIX UPDATE … WHERE assigned IS NULL → 409 |
| 5.15 | LOW | date custom field accepts any string | FIX fromisoformat |
| 5.16 | LOW | Custom field key may collide with builtin attributes | FIX reject builtin keys |
| 5.17 | LOW | Grant to deactivated department accepted; inconsistent error types | FIX reject + consistent 422 |
| 5.18 | LOW | Backfill cartesian query in conversations | FIX or_(and_(…)) pairs |
| 5.19 | LOW | Duplicate companies | P20 (dedupe UX) |
| 5.20 | LOW | list_messages offset paging without id tiebreak | FIX add id asc |

## Area 2 — SMS/MMS pipeline
| # | Sev | Item | Triage |
|---|---|---|---|
| 2.1 | CRITICAL | Twilio inbound always dead-lettered (`SmsStatus=received` treated as DLR) → STOP not honored on Twilio | FIX `if status and status != "received"` + test with SmsStatus |
| 2.2 | HIGH | Outbound MMS media never persisted (media=[]) → lost on quiet-hours release; never metered | FIX persist asset ids; rebuild URLs on release |
| 2.3 | HIGH | Held-message release dispatches through the primary carrier, not message.carrier | FIX per-row registry.get(message.carrier) |
| 2.4 | HIGH | Inverted quiet-hours window holds everything forever | FIX reject start >= end (422) |
| 2.5 | MED | Failover changes from-number but not thread → split conversations | FIX re-upsert thread to winning from_e164 |
| 2.6 | MED | Reply in thread falls to primary when sticky number is refused; sender filters is_active only | FIX raise on refused sticky for replies; filter status=="active" |
| 2.7 | MED | Bulk campaigns ride the 24h active-conversation quiet-hours carve-out | FIX skip carve-out when BULK_SEND_KEY set |
| 2.8 | MED | Campaign crash-recovery adopts unrelated messages | FIX bound window + match from/body; don't mark sent when adopted is rejected |
| 2.9 | MED | reprocess_pending assumes Bandwidth payload shape | FIX re-parse via owning adapter |
| 2.10 | MED | Plivo DLRs lack error code; unknown statuses vanish | FIX pass ErrorCode; UnknownEvent |
| 2.11 | MED | Message stranded in `queued` with hold_until NULL after crash | FIX stale-queued recovery pass in sweeper (age > 10 min → fail or resend once) |
| 2.12 | LOW/MED | Unbounded background queries + list_dnc no limit | FIX limits |
| 2.13 | LOW | Inbound MMS never expires | FIX set expires_at in _fetch_one |
| 2.14 | LOW | "Yes" confirmation without standing opt-out; single-word cancel/end | FIX only the optin confirmation gate; keep keyword set (CTIA) |

## Area 6 — Outbound engine + AI agents
| # | Sev | Item | Triage |
|---|---|---|---|
| 6.1 | HIGH | Stale-send adoption unbounded / steals unrelated messages (= 2.8) | FIX with 2.8 |
| 6.2 | HIGH | Campaign sends bypass sticky sender → forked threads, opt-out state split | FIX select_sender restricted to campaign pool |
| 6.3 | HIGH | SMS agent sends from thread.our_e164 with no active-number check | FIX via select_sender(allow_reassign) |
| 6.4 | HIGH | Stale-dial requeue has no attempt cap (infinite redials) | FIX mirror outbound cap |
| 6.5 | HIGH | Voice campaigns ignore rate_per_minute/daily_cap/warmup | FIX apply pacing gate in dialer_tick |
| 6.6 | MED | start_at dead: scheduled campaigns start immediately | FIX: /start with future start_at → status scheduled; outbound_tick releases at start_at |
| 6.7 | MED | Upload limits enforced after full read/parse | FIX: byte-counted read + abort parse at MAX_LIST_ROWS |
| 6.8 | MED | /lists/{id}/commit double-import race | FIX claim via UPDATE … WHERE status='importing' |
| 6.9 | MED | LLM `when`/`notes` unbounded → DataError → thread never answered | FIX truncate + treat DB error as retryable |
| 6.10 | MED | Final tool round discards results → empty reply + handoff | FIX final round without tools / synthesize confirmation |
| 6.11 | MED | Missing LLM key permanently hands off every thread | FIX not-configured → skipped, thread stays active; add re-arm route |
| 6.12 | MED | KB search unbounded + unescaped ILIKE | FIX escape + limit |
| 6.13 | MED | KB document text unbounded | FIX max_length 1_000_000 + chunk cap |
| 6.14 | MED | Call scoring tokens unmetered; transcript uncapped | FIX persist tokens on CallScore + usage; cap transcript chars |
| 6.15 | MED | Voice campaigns never emit campaign.completed | FIX |
| 6.16 | MED | Whole-list materialization at campaign start | FIX LIMIT 1 probe + batched inserts |
| 6.17 | MED | Import loop holds whole file + identity map | FIX expunge_all per batch (streaming parser → later) |
| 6.18 | MED | outbound_tick gated on env carrier → DB-only orgs never send campaigns | FIX gate on carrier or registry |
| 6.19 | LOW | Claim without FOR UPDATE SKIP LOCKED (outbound + dialer) | FIX (with 8.18 advisory lock) |
| 6.20 | LOW | pace_cache keyed by from_e164 only | FIX key (org_id, from_e164) |
| 6.21 | LOW | Abandoned parallel-dial leg never redialed | FIX schedule next_attempt_at |
| 6.22 | LOW | /appointments unbounded | FIX limit/offset |
| 6.23 | LOW | Phone region hardcoded US; duplicate CSV headers | ACCEPT region (P20 workspace locale); FIX duplicate-header 422 |
| 6.24 | LOW | Transcript interpolated into scoring prompt unescaped | FIX delimit + data instruction |
| 6.25 | LOW | e164 in agent request path unencoded | FIX quote |
| 6.26 | LOW | Empty job metadata runs a call with no org context | FIX validate UUID + shutdown |

## Area 3 — Voice / media plane
| # | Sev | Item | Triage |
|---|---|---|---|
| 3.1 | CRITICAL | LiveKit httpx client 5s default timeout vs 45s ring → every long ring fails + room deleted under operator | FIX Timeout(10, read=90) |
| 3.2 | CRITICAL | Inbound Telnyx flow/default commands never executed (silence) | FIX execute_commands when render_commands is None |
| 3.3 | HIGH | Inbound room leg never advanced to answered (no duration/analytics) | FIX in answer_call |
| 3.4 | HIGH | Answer race: two agents both get tokens | FIX mint only on rowcount==1 / first-claim; 409 |
| 3.5 | HIGH | softphone_token no inbox check (= 5.2) | FIX with 5.2 |
| 3.6 | HIGH | Bandwidth voice webhook guard `not user and not password` | FIX `or` |
| 3.7 | HIGH | livekit on bridge with 500 published UDP ports → hairpin/one-way audio | FIX infra: network_mode host (media-plane restart) |
| 3.8 | MED-HIGH | Inbound trunk number exact-match, no normalization/fallback | FIX to_e164 + sip.callTo fallback |
| 3.9 | MED-HIGH | dialer awaits the test-only global pending-dial set | FIX await own task |
| 3.10 | MED | decline() hangs up the whole group | FIX local decline |
| 3.11 | MED | single audio element → one party audible in 3-party rooms | FIX per-track attach/detach |
| 3.12 | MED | ring_user_ids ignored → sequential groups ring everyone | FIX client + _event_visible filter |
| 3.13 | MED | dial_callback_now double-dial | FIX conditional UPDATE + 409 |
| 3.14 | MED | fetch_pending_recordings commits once across orgs | FIX commit per row |
| 3.15 | MED | Telnyx execute_commands swallows failures → fake hangup 200 | FIX raise → 502 |
| 3.16 | MED | transport errors classified as no_answer | FIX only real twirp responses |
| 3.17 | MED | join tokens 1h TTL | FIX 120s |
| 3.18 | LOW-MED | fe.step in _drive not wrapped → dead air instead of fallback | FIX |
| 3.19 | LOW-MED | create_outbound_call commits after carrier I/O | FIX commit queued rows first |
| 3.20 | LOW-MED | Bandwidth event id collides on same-timestamp events (lost DTMF) | FIX payload discriminator |
| 3.21 | LOW | adopt_transfer_leg by to_e164 only | FIX include call/thread scoping |
| 3.22 | LOW | start_room_call returns 201 after create_room failure | FIX return failed |
| 3.23 | LOW | overnight business hours unsupported (D18) | FIX evaluate_hours wrap + validate weekdays |
| 3.24 | LOW | webhook batch shield continues without rollback | FIX rollback |
| 3.25 | LOW | recording_url fetched without host allowlist (non-Bandwidth) | FIX allowlist per carrier |

## Area 7 — Frontend (64 findings; full text in the review output)
FIX NOW (bug-fix round): 1 cache clear on logout/org switch · 2 polling for list+timeline (+ backend `message.received` bus event) · 3 notes → /contacts/{id}/notes · 5 answer error/restore + gating on calls:place · 6/7 flow transfer nodes + rename collision · 8 error states everywhere · 9 composer key · 10 invalidate off variables · 11/12 null thread_id · 13 legacy inbox viewer gating · 14 readOnly wiring · 15 caller-id restore · 16/17 mute/hangup ordering · 18 softphone reset on org switch · 19 dirty-guard · 20 via for dispatch · 21 handle sms.handoff/callback events · 22 localStorage try/catch + ErrorBoundary · 24 accept-invite when authed · 26 stop reconnect on 4401 · 27 resync on reconnect · 28–30 timer/dtmf/ringtone resume · 32 in-flight disable · 34 clear params on org switch · 35/36 types · 37 debounce · 38 invalidate available · 39 add-number pending · 40 2FA page (QR/link, disable, busy) · 41 recording fetch via client · 42 disable for viewers · 43 flow selects · 44/45 · 46 regenerate types · 47 confirm with cancel+timeout · 48 confirms · 49 · 51 textarea+segments · 52–55 a11y · 59–61 · 64.
P20: 4 full RBAC nav (backend `permissions` on /auth/me ships now) · 23 org switcher · 25 token refresh · 31 · 33 · 50 · 56–58 · 62 · 63.

## Area 1 — Auth / RBAC / tenancy / webhook ingress
| # | Sev | Item | Triage |
|---|---|---|---|
| 1.1 | BLOCKER | `POST /numbers` accepts any e164 → hijack another tenant's inbound | FIX: verify ownership via the org's provider (lookup_owned_number on each NumberProvider); unverifiable → 422 |
| 1.2 | HIGH | TOTP replay within ±1 step | FIX record step+window; reject <= last |
| 1.3 | HIGH | Own carrier creds verify webhooks attributed to any org | FIX pin ingest to verifying account's org; dead-letter mismatch |
| 1.4 | HIGH | No member remove / role update | FIX routes (members:remove/update) + audit; UI in P20 |
| 1.5 | HIGH | Existing user can't accept an invite to a second org | FIX POST /invites/accept (authed) |
| 1.6 | MED | Registration status self-attestable | FIX: status routes require platform-operator token (PLATFORM_OPS_TOKEN) — tenants submit only |
| 1.7 | MED | Bandwidth voice guard AND (= 3.6) | FIX with 3.6 |
| 1.8 | MED | API keys minted with scopes beyond creator's role | FIX subset check |
| 1.9 | MED | 2FA enroll/disable without password | FIX require password |
| 1.10 | MED | Malformed voice webhook → silent hangup, no dead-letter | FIX dead_letter |
| 1.11 | MED | delivery_tick unexpected exception aborts pass | FIX bare except → failed |
| 1.12 | MED | Cross-tenant deletes not guarded | FIX include session.deleted |
| 1.13 | MED | No rate limiting anywhere (auth/register/2fa/api-key) | FIX in-process limiter middleware on /auth/* + failed key auth (with 8.3) |
| 1.14 | LOW | Plivo verify no empty-token guard | FIX |
| 1.15 | LOW | Twilio/SignalWire no replay window | FIX seen-signature TTL cache |
| 1.16 | LOW | CORS * with credentials reachable | FIX prod guard |
| 1.17 | LOW | Disabled account distinguishable 403 | FIX uniform 401 |
| 1.18 | LOW | /docs + openapi public | FIX off in production |

## Area 4 — Numbers / providers / spend / sweeper
| # | Sev | Item | Triage |
|---|---|---|---|
| 4.1 | HIGH | Campaign, AI-reply and auto-reply sends bypass the 10DLC/TFV gate | FIX gate in send_message when plan is None + eligibility filter in outbound pool |
| 4.2 | HIGH | Sweeper send/fetch paths run with CURRENT_ORG_ID=None → env registry | FIX set+prime per org in every hook |
| 4.3 | HIGH | Undecryptable account → every request 503s | FIX guard prime (log, degrade) |
| 4.4 | HIGH | Telnyx provider_ref mixes order id vs phone-number id | FIX store order id; resolve number id at release |
| 4.5 | HIGH | Hand-added toll-free typed local skips TFV | FIX derive number_type from prefix |
| 4.6 | HIGH | Failed orders billed MRC/setup forever | FIX status in (active, released) |
| 4.7 | HIGH | Rollup collision aborts the whole pass (spend/usage/reputation) | FIX per-org try/except+rollback |
| 4.8 | HIGH | Purchased number orphaned on DB conflict | FIX pre-check + release on IntegrityError |
| 4.9 | HIGH | routing probe uses env creds not org account | FIX |
| 4.10 | MED | prime TOCTOU on version | FIX capture first |
| 4.11 | MED | bump_version before commit | FIX bump after commit |
| 4.12 | MED | PATCH creds keeps active | FIX → unverified |
| 4.13 | MED | create/revive doesn't invalidate webhook cache | FIX |
| 4.14 | MED | Poll starvation across orgs; no attempt ceiling | FIX round-robin + cap + max attempts |
| 4.15 | MED | Per-process gates/version under multi-worker | FIX with 8.18 advisory lock |
| 4.16 | MED | usage (ended_at) vs spend (created_at) day bucket | FIX ended_at both |
| 4.17 | MED | Webhook fallback limit 50 without ORDER BY | FIX order + paginate |
| 4.18 | MED | TTL lapse sends DB-only org via env carrier | FIX empty registry when org has DB accounts |
| 4.19–4.29 | LOW | quoting, TransportError, carrier validation on add, write amplification, healthz/status leakage, cache expiry, rate map, released numbers in reputation, audit field lists, LRU, primary preference | FIX all (cheap) |
