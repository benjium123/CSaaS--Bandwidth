# CSaaS Runbook

Operational reference for a deployed instance. Written for the operator at 2am, not for a
new contributor learning the architecture - see `docs/ARCHITECTURE.md` and
`docs/PROGRESS.md` for that. Everything here assumes the standard on-box layout:

```
/opt/csaas/                       # REMOTE_DIR - deploy.sh touches nothing outside this
  .env                            # secrets, never in git, never overwritten by deploy.sh
  deploy/docker-compose.prod.yml  # compose project name "csaas"
  backend/scripts/                # backup.sh, restore_drill.sh, smoke_restore.py, load_test.py
  backups/                        # created by backup.sh, chmod 700
```

Compose project name is `csaas` (`name: csaas` in `docker-compose.prod.yml`), so container
names are `csaas-api-1`, `csaas-db-1`, `csaas-redis-1`, and the built api image is tagged
`csaas-api`.

---

## Deploy

### Before the very first deploy to a box

```bash
ssh root@144.126.152.175 "ls -la /opt/csaas"
```
D11: `deploy.sh` ships files via `rsync -a --delete` into `/opt/csaas` (see "What
deploy.sh ships" below) - `--delete` removes anything under `/opt/csaas` that isn't part
of this deploy's payload. On a fresh box that's harmless; on a box that already has
*anything* in `/opt/csaas` from another source, inventory it with the command above
first, so an unexpected deletion doesn't come as a surprise the first time you run this.

Also required before the first deploy, both one-time, both operator-run (not done by
`deploy.sh`):
- **nginx rate-limit zone**: `deploy/nginx-csaas-limits.conf` (a single `limit_req_zone`
  directive) must be installed to `/etc/nginx/conf.d/csaas-limits.conf` **before**
  `deploy/nginx-csaas.conf` is enabled - the site file's `limit_req zone=csaas_auth ...`
  lines (login/2FA/register) fail `nginx -t` if the zone isn't already defined in an
  http-context file. `conf.d/*.conf` is included from nginx.conf's top-level `http{}`
  block on stock installs; verify with `nginx -t` before `systemctl reload nginx`, same
  as any other nginx change on this shared box.
- **LiveKit redis password**: `deploy/livekit/livekit.yaml` and `.../sip.yaml` are
  git-ignored, generated files - `deploy.sh` renders them from their `.tpl` counterparts
  after every rsync, reading `CSAAS_REDIS_PASSWORD` from `/opt/csaas/.env`. See
  `deploy/livekit/README.md` section 1b for the full explanation and the local-dev
  rendering command. Nothing to do here if `CSAAS_REDIS_PASSWORD` is already set in
  `.env` - it's automatic - but know that this render step exists before debugging a
  livekit config that "reverted itself."

### If you deployed before this fix (D2): fix `csaas_media` ownership once

`deploy/Dockerfile` now creates `/app/var/media` (owned by `csaas`) before the image's
final `chown -R`, so a brand-new `csaas_media` named volume inherits the right
ownership on first use. If the box already deployed with the old Dockerfile, the
existing `csaas_media` volume was seeded root-owned instead - the `api` container
(running as `csaas`) can't write media/recordings/transcripts into it. Only relevant if
the volume is already empty (nothing has been written to it yet - check with
`docker run --rm -v csaas_media:/m alpine ls -la /m`); if it already holds real data,
fix ownership in place instead (`docker run --rm -v csaas_media:/m alpine chown -R
10001:10001 /m`) rather than deleting it.

```bash
docker compose -f deploy/docker-compose.prod.yml down          # stop the container using it
docker volume rm csaas_media                                   # ONLY if empty - see above
./deploy/deploy.sh                                              # recreates it correctly on next `up`
```

### Running it

```bash
./deploy/deploy.sh [user@host]     # defaults to root@144.126.152.175
```

Pre-flight checked, idempotent, safe to re-run. It builds the console locally (node never
runs on the box), ships tracked files via `git archive HEAD` (D11: this - not the local
working tree - is what actually gets deployed, so **uncommitted changes never reach the
box**; commit or `ALLOW_DIRTY_FRONTEND=1` for the frontend, see the script's own comments),
brings the stack up with `docker compose ... up -d --build`, runs `alembic upgrade head`
inside the api container, and checks `/healthz`. It NEVER writes `.env` - if
`/opt/csaas/.env` is missing, it aborts and tells you to create it by hand from
`.env.example`. Pre-flight also refuses to proceed if `CSAAS_DB_PASSWORD` or
`LIVEKIT_API_SECRET` is missing/empty in that `.env` - checked before anything (including
migrations) runs, not after.

## Rollback

Migrations are **additive by policy** (P0 architecture decision - a column is added, never
dropped, in the same release that stops using it), so a rollback never needs
`alembic downgrade`:

```bash
git revert <bad-commit>            # or: git checkout <last-good-tag>
./deploy/deploy.sh                 # redeploy - same idempotent path as a forward deploy
```

If a migration genuinely must be reverted (should not happen under the additive policy),
that is a Tier-1 decision - do not run `alembic downgrade` against the live database
without restoring to a throwaway copy first and rehearsing it there (see the restore drill
below - the exact same throwaway-container pattern works for rehearsing a downgrade).

---

## Backups

`backend/scripts/backup.sh` runs ON THE BOX. It `docker exec`'s into the running
`csaas-db-1` container, `pg_dump -Fc`'s over the container's own local socket (no network
hop, no password needed), writes to `/opt/csaas/backups/csaas-YYYYmmdd-HHMM.dump`
(`chmod 600`), and prunes to the newest 14.

**`deploy.sh` deliberately does NOT install the cron job** - it promises to touch nothing
outside `/opt/csaas`, and a cron line lives in root's crontab. Install it once, by hand:

```bash
ssh root@144.126.152.175
mkdir -p /opt/csaas/backups && chmod 700 /opt/csaas/backups   # D15: backup.sh assumes this exists
crontab -e
# CRON_TZ makes the schedule explicit regardless of the box's system TZ - containers (and
# most VPS base images) default to UTC, so "30 3" without this fires at 03:30 UTC, i.e.
# 22:30 or 21:30 America/Chicago the PREVIOUS day, not 03:30 CT. Needs a cron that supports
# per-line CRON_TZ (vixie-cron/cronie do); if yours does not, set TZ=America/Chicago instead.
# D14: unlike TZ (which shifts the WHOLE crontab), CRON_TZ is NOT scoped to just this one
# line - it applies to every subsequent line in the crontab until a later CRON_TZ/TZ
# assignment resets it. If you add more cron lines below this one and want them on the
# box's default TZ again, assign CRON_TZ (or TZ) back explicitly before them.
CRON_TZ=America/Chicago
30 3 * * * /opt/csaas/backend/scripts/backup.sh >> /opt/csaas/backups/backup.log 2>&1
```

Verify it landed: `crontab -l | grep backup.sh`.

## Restore drill

`backend/scripts/restore_drill.sh` proves the newest backup actually restores and serves
the app - entirely inside THROWAWAY containers (its own docker network, tmpfs postgres
data, a container name namespaced with its own PID). It **never touches the live compose
project or the live database, and it reads NOTHING from `/opt/csaas/.env`** - it does not
need to. The throwaway postgres boots with a FRESH, RANDOM, drill-only password generated
in the script (`head -c 24 /dev/urandom | base64 | tr -d '/+='`); it never needs to match
anything live, because `pg_dump -Fc` carries no role passwords and the restore runs
`--no-owner`. A live credential is deliberately never read into a shell variable here, so
it can never show up in `ps` / `docker inspect` output on a box shared with other tenants.

The drill's docker network (`csaas-drill-net-<pid>`) isolates the throwaway postgres from
the LIVE compose network only - two containers cannot see each other by name across
separate docker networks. It is **not egress isolation**: a container on the drill network
can still reach the internet like any other container on the host. Nothing in the drill
needs outbound access, but do not rely on the network for anything stronger than "the live
`csaas-db-1` and this throwaway postgres cannot address each other."

```bash
cd /opt/csaas
./backend/scripts/restore_drill.sh                 # restores the newest dump
./backend/scripts/restore_drill.sh /path/to/x.dump  # or a specific one
```

Steps it runs, in order: spin up throwaway postgres -> `pg_restore` the dump into it ->
`alembic upgrade head` against it FROM THE API IMAGE (must be a no-op - if it applies any
migration, the restored data is behind the schema this image expects, which is a real
finding, not a drill bug) -> `scripts/smoke_restore.py` against it (row-count sanity on
`orgs`/`users`/`roles`, a tenant-isolation spot check driven through the app's own session
machinery) -> destroy the throwaway container and network (a `trap` runs this on ANY exit,
including failure).

**Run this after every backup.sh change, after every migration that touches a table
`smoke_restore.py` checks, and periodically as a standing drill** (a backup nobody has ever
restored is a hope, not a backup).

`scripts/smoke_restore.py` can also be pointed at any database directly:

```bash
DATABASE_URL=postgresql+asyncpg://csaas:PASS@host:5432/csaas python scripts/smoke_restore.py
python scripts/smoke_restore.py --self-test    # DB-free: validates the assertion helpers themselves
```

---

## Carrier failover: manual override & breaker interpretation

Failover is automatic in the send path (P14 DR-1/DR-2/DR-3) - a carrier-fault error
(`carrier_transient`, `carrier_unreachable`, `rate_limited`, or `auth` - a dead/rotated
credential) walks the routing plan to the next healthy candidate in the SAME request, and
the breaker recovers on its own after a 30s cooldown via one half-open probe. Nothing below
is required for normal operation; it exists for when an operator needs to intervene.

**See live carrier health** (authenticated, `settings:read`):

```bash
curl -H "Authorization: Bearer $TOKEN" -H "X-Org-Id: $ORG_ID" \
  https://HOST/api/v1/routing/carriers
```

Each entry: `name`, `primary`, `state` (`closed` = healthy / `open` = tripped, refusing new
sends except probes / `half_open` = cooldown elapsed, next send is the one probe),
`consecutive_failures`, `capabilities`. Never includes a credential.

**Probe a carrier's credentials directly** (`settings:write` - operator-triggered only,
never runs on boot):

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "X-Org-Id: $ORG_ID" \
  https://HOST/api/v1/routing/carriers/{name}/probe
```

**Pin / unpin a carrier** (forces every send to one carrier regardless of health ranking -
use during a known outage on a carrier that keeps flapping healthy just long enough to
re-attract traffic):

```bash
# pin
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "X-Org-Id: $ORG_ID" \
  -H "Content-Type: application/json" -d '{"pinned_carrier": "telnyx"}' \
  https://HOST/api/v1/routing/policy

# unpin (back to preference order / automatic health ranking)
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "X-Org-Id: $ORG_ID" \
  -H "Content-Type: application/json" -d '{"pinned_carrier": null}' \
  https://HOST/api/v1/routing/policy
```

**Enable/disable cross-carrier failover for an org** (default OFF - a carrier switch
changes the sender the recipient sees, so it is opt-in per org):

```bash
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "X-Org-Id: $ORG_ID" \
  -H "Content-Type: application/json" \
  -d '{"allow_cross_carrier_failover": true}' \
  https://HOST/api/v1/routing/policy
```

Note the one refusal that is BY DESIGN and not a bug: a reply inside an existing thread
never crosses carriers, even with `allow_cross_carrier_failover: true` - the recipient has
already seen a sender, and a stranger answering that conversation is worse than the message
not sending. Intra-carrier failover (a second number on the SAME carrier) still applies.

**Voice does not fail over today.** `services/calls.create_outbound_call` dials exactly one
(carrier, from) and does not retry elsewhere on rejection - see `docs/PROGRESS.md`'s P14
entry / `tests/test_failover.py::test_voice_create_call_does_not_fail_over_today` for the
pinned current behaviour and what a future phase would need to add.

---

## Number reputation

`GET /api/v1/numbers/reputation` (`reports:read`) returns trailing-7-day, per-number
delivery rate, carrier-error rate, spam-class error count, and volume - derived entirely
from our own `messages` rows, no third-party API. The sweeper writes an `audit_log` row
(`action = "number.reputation_alert"`) at most once per (org, number, UTC day) when a
number's delivery rate falls below 85% over at least 50 sends, or it records ANY
spam-class carrier error. There is no alerting beyond that audit row in this phase - watch
it via the audit log endpoint, or query `audit_log` directly.

---

## Load testing

`backend/scripts/load_test.py` - asyncio + httpx, zero new dependencies.

```bash
# read-only traffic against the console's own endpoints
python scripts/load_test.py --base-url https://HOST --token $TOKEN --org-id $ORG_ID \
  --rps 20 --seconds 30 --mode read

# send traffic - REFUSES to run unless every number on the org is on the loopback
# carrier (verified LIVE against the API) or you pass --i-know-this-is-loopback
python scripts/load_test.py --base-url https://HOST --token $TOKEN --org-id $ORG_ID \
  --rps 10 --seconds 20 --mode send

# no server needed at all - in-process burst against the ASGI app directly
python scripts/load_test.py --self-test
```

**Pass bar:** p95 < 250ms @ 20 rps sustained, error rate 0. Record each run's numbers here
(or in `docs/PROGRESS.md`'s session log) when it is executed against the VPS - a load test
that is not recorded did not happen for the next person reading this file.

---

## B1-B4: unblock steps

These are the standing, non-code blockers as of the last PROGRESS.md update - check that
file for current status before acting on this list, it may have moved.

| # | Blocker | Unblocks | Action |
|---|---|---|---|
| B1 | No messaging-capable carrier (Bandwidth account is Voice + Numbers only) | P1b, P4 registration, P10's live SMS turn | Get a Telnyx API key + 10DLC brand/campaign, set `TELNYX_API_KEY` / `TELNYX_MESSAGING_PROFILE_ID` in `/opt/csaas/.env`, restart the api container. |
| B2 | No SIP trunk points at the box | P5 voice runtime | Configure a Bandwidth voice application -> Inbound SIP peer with the box's IP `144.126.152.175:5060`, assign the voice number to it. |
| B3 | Media plane bring-up | P6/P7/P8/P9 (media plane, echo, voice agent) | See "Media plane bring-up" below - this is now the canonical location for that procedure (moved from PROGRESS.md). |
| B4 | No AI provider keys in production `.env` | P8/P9 voice agent, P10 SMS agent's LLM turn | Paste `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` into `/opt/csaas/.env`, restart the api container. |

---

## Media plane bring-up (B3)

```bash
ssh root@144.126.152.175
ufw allow 7881/tcp comment 'csaas livekit ice-tcp'
ufw allow 50700:51199/udp comment 'csaas livekit rtp'
ufw allow 5060/udp comment 'csaas sip signaling'
ufw allow 10000:10499/udp comment 'csaas sip rtp'
# D11: `livekit` runs network_mode: host (deploy/livekit/docker-compose.livekit.yml), so
# there is no docker-proxy port-publish to restrict 7880 to loopback the way other
# services get it - `bind_addresses: [""]` in livekit.yaml means 7880 listens on every
# host interface, public IP included, unless ufw blocks it. Apply this deny rule BEFORE
# bringing `livekit` up. If the softphone needs LiveKit reachable publicly, do that via
# the nginx wss proxy in "Operator step: expose /status and the LiveKit WS through
# nginx" below FIRST, and repoint LIVEKIT_PUBLIC_URL to that wss:// URL - see the caveat
# there - because this deny rule cuts off the direct ws://<ip>:7880 form entirely.
# The api container reaches livekit at 7880 through the docker host-gateway, which is
# INPUT traffic on the host as far as ufw is concerned - with a default-DROP policy the
# deny alone leaves /status reporting media_plane: down (seen live 2026-09-09). Allow the
# compose network's subnet first (`docker network inspect csaas_default` prints it):
ufw allow in from 172.24.0.0/16 to any port 7880 proto tcp comment 'csaas api -> livekit signal'
ufw deny in on <public-iface> to any port 7880 proto tcp comment 'csaas livekit signal loopback-only'
cd /opt/csaas && docker compose --env-file .env \
  -f deploy/docker-compose.prod.yml \
  -f deploy/livekit/docker-compose.livekit.yml up -d livekit livekit-sip
```

Verify the target ports are free (nothing listening on 7880/7881/5060) BEFORE running this
- the RTP ranges above are deliberately narrow because the VPS hosts other tenants' services
alongside csaas.

Verify the deny rule actually took (D11):
```bash
ss -ltnp | grep 7880          # on-box: confirm livekit IS listening (sanity check)
curl -s 127.0.0.1:7880        # on-box, over loopback: MUST succeed (nginx/local tools still work)
# from a DIFFERENT machine (never the VPS itself):
nc -z 144.126.152.175 7880 && echo "REACHABLE - deny rule did not take, fix before continuing" \
  || echo "not reachable - deny rule confirmed"
```

`deploy/livekit/README.md` documents the trunk setup as **Telnyx**; with the Bandwidth/
Telnyx split (B1), voice is on **Bandwidth**, so the inbound trunk step takes Bandwidth's
signaling hosts, not `sip.telnyx.com`.

Residual items as of the last time this ran (check `docs/PROGRESS.md` for current state):
nginx needs a `wss` proxy location for port 7880 before the browser softphone can connect
(additive change to the shared csaas nginx site - get authorization before touching a
config file nginx shares with other tenants). The AI worker item is DONE in P38: see
"AI worker + media-plane headroom (P38)" below.

---

## AI worker + media-plane headroom (P38)

The AI agent no longer needs its own separate worker service or venv: it is now the `agent` service in `deploy/livekit/docker-compose.livekit.yml`.

Operator steps, in order:

1. Put the AI keys into `/opt/csaas/.env`: `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ANTHROPIC_API_KEY`. Add `OPENAI_API_KEY` only if `LLM_PROVIDER=openai` is wanted. The operator writes `.env`, not us.
2. Add the two firewall rules (re-check with `ss -lun` first):
   ```bash
   ufw allow 51200:52699/udp comment 'csaas livekit rtp (P38 widen)'
   ufw allow 10500:11999/udp comment 'csaas sip rtp (P38 widen)'
   ```
3. Read the Telnyx SIP connection concurrent-channel limit in the portal (Voice, SIP Trunking, the FQDN connection, Inbound/Outbound settings). Record the value here. If it is under about 50, raise it there.
4. `bash deploy/deploy.sh`
5. Off-hours only, restart the media services so the widened ranges take effect. Live calls drop:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml -f deploy/livekit/docker-compose.livekit.yml restart livekit livekit-sip
   ```

Checks:
- `docker logs csaas-agent-1` shows the worker registering as `agent_name=ai-agent`.
- One outbound AI call to your own phone via the API: the call answers, the AI speaks within about 2 seconds, and hangup writes the `/agent/outcome` row.
- Run the conversation-replay gate: `agents/replay_harness.py` (see the README). Want rt at or above 0.97, zero underruns, `tail_energy_ratio` at or above 0.5.
- During a test call, `ss -lun` shows RTP binding inside the new ranges (10000-11999 and 50700-52699).
- `docker stats` shows the worker under its 4-core cap and the other tenants' containers unaffected.

Capacity: expect roughly 10-25 concurrent AI calls per 4 cores on this shared box. When AI calls pass about 10, the next step is a second worker VPS, not a bigger LiveKit.

Rollback:
1. Remove the `agent` service and run `compose up -d`; the worker is gone and nothing else changed.
2. Revert `livekit.yaml.tpl` `port_range_end` to 51199 and `sip.yaml.tpl` `rtp_port.end` to 10499.
3. Off-hours, restart `livekit` and `livekit-sip`.

---

## SignalWire go-live (P39)

Order matters. Steps 1 and 2 are SignalWire dashboard work; the rest is on the box.

1. Rotate the SignalWire API token in the dashboard first. The current one has been pasted into chat and shown in a screenshot. Use the new token in step 3.
2. Confirm 10DLC: Messaging → Campaign Registry → the campaign is *approved* and BOTH numbers are assigned to it. Without this SignalWire rejects outbound texts (inbound still works). Capability icons are not the campaign.
3. `/opt/csaas/.env`:
```bash
SIGNALWIRE_PROJECT_ID=9c2ec6d5-b851-4091-8b4b-c7ce0a845f87
SIGNALWIRE_API_TOKEN=<new token>
SIGNALWIRE_SPACE_URL=sabine.signalwire.com
SIGNALWIRE_WEBHOOK_URL=https://csaas.sabinepropertygroup.net/api/v1/webhooks/signalwire/messaging
```
Bare host for the space, no scheme, no trailing slash; the SSRF guard rejects anything else. `SIGNALWIRE_ENABLED` can stay unset, the credentials enable it.
4. `bash deploy/deploy.sh` (ships migrations 0041-0043, the voice adapter, and this phase).
5. `bash deploy/signalwire_number_webhooks.sh 08ede4ed-ab9a-40bc-a94b-45fe39459a0c b724ed20-2ba7-4e34-aaee-44a97ecbf169` on the box. Then check each number's page: "Handle messages using" / "Handle calls using" must read "LaML Webhooks".
6. Numbers page → Add number → `+16824231003`, then `+14692103654`. Each should appear with carrier **signalwire** and get its inbox.

Checks:
- Text the 682 number from a phone → thread appears in the inbox within seconds; `docker logs csaas-api-1` shows a verified webhook, no `signature_mismatch`.
- Send from the 682 number to the operator's phone → received, and shows **delivered** within ~10 s, not just sent.
- Repeat both for 469.

### Calling on these numbers (P40)
Calls go SignalWire <-> livekit-sip <-> LiveKit rooms, same as Telnyx: the softphone, the dialer and the AI assistant all work from a signalwire number, and the call is billed at the signalwire rate card. A call from a number on a carrier with no trunk still uses the Telnyx trunk, as before. Setup: `deploy/livekit/README.md` step 5c.

Checks after setup:
- Softphone -> call your mobile FROM +1 682 423 1003 -> it rings, the caller id reads 682 423 1003, audio both ways. `docker logs csaas-api-1` has no `livekit_dial` error; the Calls page shows carrier **signalwire**.
- Call the 682 number from a mobile -> the call rings in the console (`lk room list` shows a `call-` room). Answer it; audio both ways.
- Repeat for 469.
- Outbound fails with 401/407 in `docker logs csaas-livekit-sip-1` -> wrong trunk username/password. Inbound never arrives -> the number's call handler is not the inbound SWML script, or UDP 5060 is blocked.

Rollback: unset the four `SIGNALWIRE_*` values and redeploy. Delete the numbers from the Numbers page. The code changes are additive and inert without credentials.

---

## Trust & safety go-live (P41)

Order matters - do these before switching enforcement on.

1. **Stripe Identity.** In the Stripe dashboard enable Identity. Add the webhook events
   `identity.verification_session.verified`, `.requires_input`, `.processing`, `.canceled`
   to the existing endpoint `https://<api>/api/v1/webhooks/stripe` (or a separate endpoint,
   then set `STRIPE_IDENTITY_WEBHOOK_SECRET`). `STRIPE_SECRET_KEY` needs Identity read access
   with verified outputs.
2. **Keys.** `CREDENTIALS_MASTER_KEY` must be set (business documents are encrypted with it).
   Optional: `COMPANIES_HOUSE_API_KEY` (free, UK registry), `GEOLITE2_DIR` (free MaxMind
   GeoLite2 Country + ASN files), `SMTP_HOST` + `SMTP_FROM` (alert and decision emails).
3. **Deploy.** `alembic upgrade head` applies 0044 + 0045. 0045 marks every existing org
   `approved` (reason `grandfathered`), so live traffic keeps flowing.
4. **Operators.** Each reviewer signs in once, adds a passkey or authenticator app, then:
   `docker compose exec api python scripts/make_operator.py grant you@company.com admin`
   (`reviewer` for people who only review). The console is at `/ops` in the web app.
5. **First sweep.** The sweeper downloads the sanctions lists and Tor exit list within its
   first pass after start (needs outbound HTTPS). Until then the sanctions check answers
   `error` and approvals are blocked - that is intended.
6. **Enforce.** `REQUIRE_2FA_ALL_USERS=true` and `KYC_ENFORCED=true` (both default on).
   Users without a second factor are sent to "Secure your account" on next sign-in.
7. **Smoke test.** Create a test workspace, complete verification with Stripe test-mode
   documents, approve it from `/ops`, send one text, then suspend it and confirm the owner's
   session ends and texting answers `account_suspended`.

### Reviewing an application
- Queue: `/ops` > Review queue. High-risk applications show why.
- US and Canada registry: look the business up (Secretary of State / Corporations Canada),
  paste the page link, press "Registry: confirmed" or "not found".
- High risk: hold a short video call with the owner holding their ID, then "Record video call done".
- Approve is blocked until: every owner's ID is verified, sanctions and ban-list checks
  pass, registry is recorded, and (high risk) the video call is recorded.
- Reject with "also ban identifiers" puts the company number, domains, emails, card
  fingerprints, devices and verified people on the ban list.

### Suspending
`/ops` > application > reason > "Suspend account now" (admin operators; asks for a fresh
passkey/authenticator check). Ends sessions, revokes API keys, cancels scheduled texts,
pauses campaigns, hangs up live calls, emails the owners.

## Enterprise auth go-live (P42)

1. **Redis.** Production refuses to start without `REDIS_URL`. Rate limits, SSO state and
   the SAML replay store live there, shared by every worker.
2. **Email (Resend).** `SMTP_HOST=smtp.resend.com`, `SMTP_PORT=587`, `SMTP_USERNAME=resend`,
   `SMTP_PASSWORD=<Resend API key>`, `SMTP_FROM=security@<your verified domain>`. Production
   refuses plaintext SMTP. Password resets, invites, lockouts and security notices use it.
3. **Proxy.** `TRUSTED_PROXY_COUNT=1` behind the single nginx. If a load balancer also sits in
   front, set 2 - a wrong value lets callers fake their IP (IP allowlists, lockout, risk).
4. **Deploy.** `alembic upgrade head` applies 0046-0049. Reload nginx (new `limit_req` paths
   and console CSP).
5. **Cookie cut-over.** First deploy with `AUTH_BEARER_COMPAT=true` so open console tabs keep
   working; the console switches to cookies on next load. After a day, set it to `false`
   and restart. Scripts should use API keys, never user tokens.
6. **Passkeys.** Owners, admins, billing and operators see a banner for
   `PASSKEY_GRACE_DAYS` (14), then must sign in with a passkey. Tell them before deploying.
7. **SSO customers already on OIDC.** SSO stops signing people in until the workspace
   verifies its domain: Settings > Security > Verified domains > add the TXT record >
   "Check DNS". Password sign-in keeps working meanwhile (enforcement pauses too).
8. **Smoke test.** Forgot password -> email -> reset -> sign in still asks for the second
   factor. Ten wrong passwords -> "account locked" email -> unlock from `/ops`.

### Setting up SAML for a customer
- Customer verifies their domain first (above).
- Settings > Security > SAML single sign-on shows the Entity ID, ACS URL and metadata URL to
  paste into Okta / Entra ID / Google. NameID or an `email` attribute must be the email;
  optional `displayName` and `groups`. Signing: SHA-256; sign the assertion (preferred) or
  the response. Encryption off.
- Paste the IdP entity ID, sign-in URL and signing certificate, save. Test with
  `https://<web>/api/v1/auth/sso/<slug>/start` (the normal "Sign in with SSO" link).
- Refusals are logged as `saml_login_refused` with a `reason` (e.g. `wrong_audience`,
  `expired`, `replayed`, `unsigned`).

### Setting up SCIM (user sync)
- The workspace owner creates a token in Settings > Security > User sync (needs a fresh
  2FA check). Base URL `https://<api>/scim/v2`, auth "Bearer token".
- People can only be created on the workspace's verified domains. Deactivating someone in
  the IdP removes them from the workspace and ends their sessions at once. Owners can't be
  removed or re-roled through SCIM. Revoke a token in the same card.

### Locked out / lost factors
- Lockout: `/ops` > Users > search email > Unlock (admin operator, fresh 2FA).
- Agent lost their phone: a workspace admin uses "Reset 2FA" on the member.
- Owner/admin lost everything: "Lost access" on the sign-in page -> ID + selfie matching
  their verified identity. If that fails, operator "Reset 2FA" in `/ops` after checking
  identity out of band (video call with ID). Every reset is audited and emailed.

## AI safety go-live (P43)

1. **DeepSeek key.** `DEEPSEEK_API_KEY=sk-...` in the server `.env` (the key needs the `sk-`
   prefix). Check it: `docker compose exec api python scripts/monitor_exam.py` - expect PASSED
   (catch rate >= 95%, false alarms <= 3%). Add DeepSeek to the privacy policy / customer
   agreement as a data processor before switching monitoring on.
2. **Deploy.** `alembic upgrade head` applies 0050 + 0051. New Python deps: pillow, pypdfium2.
3. **Canada registry (free).** Create an account at api.ised-isde.canada.ca, subscribe to
   Federal Corporation API -> Public Plan, set `ISED_API_KEY`. Without it Canadian companies are
   confirmed from their uploaded documents.
4. **Call listener.** Rebuild and start the `call-monitor` service from
   `deploy/livekit/docker-compose.livekit.yml` (same image as the AI agent; needs
   `DEEPGRAM_API_KEY` and `ELEVENLABS_API_KEY`). Smoke test: place a softphone call from a new
   workspace, hear the announcement, hang up after 30 s, and within ~3 minutes the call shows a
   review in `/ops` > Monitoring. `DEEPGRAM_API_KEY` is also needed for recorded carrier calls.
5. **Watch mode first (recommended).** For the first days set `MONITOR_PAUSE_SCORE=100000` and
   `MONITOR_RESTRICT_SCORE=100000` so nobody is paused while you check `/ops` > Monitoring for
   false alarms; texts are still screened. Then set them back to 100 / 60.
6. **Applications already in review.** They can't be approved until each owner adds a home
   address and a proof of address (the documents check says so). Use "Ask for more info" - the
   AI decision pack pre-fills the request.

### Every day (5 minutes)
- `/ops` > Monitoring: the canary and exam pills must be green. Red = open the
  `monitor_health` alert; if DeepSeek is down, texts from new accounts are waiting, not lost.
- Flagged accounts: open each paused one, read the AI case file and the business's explanation,
  then "False alarm - unpause" or "Confirmed - suspend and ban". Both teach the monitor.
- Held texts: release or block anything the second look couldn't decide.
- Review queue: open each application, read the AI decision pack, click approve / ask for info /
  reject.

### Changing a prompt, the model or the rules
Run `scripts/monitor_exam.py --labels` before and after. Don't ship a change that lowers the
catch rate or raises false alarms.

## Incident quick-checks

No `journalctl` here - everything runs in Docker, so:

```bash
# is everything up
docker compose -f deploy/docker-compose.prod.yml ps

# tail logs (structlog JSON - pipe through `jq` if installed)
docker compose -f deploy/docker-compose.prod.yml logs -f api
docker compose -f deploy/docker-compose.prod.yml logs -f --tail 200 db

# liveness + DB reachability (no auth - monitors hit this)
curl -fsS http://127.0.0.1:8080/healthz

# public status surface: overall + per-component up/degraded/down/unconfigured, cached
# in-process for 15s. api/db/redis/per-carrier breaker state/media_plane - names only,
# never a version, count, hostname, or carrier account detail.
curl -fsS https://HOST/status

# authenticated carrier detail (which carrier, consecutive_failures, capabilities)
curl -H "Authorization: Bearer $TOKEN" -H "X-Org-Id: $ORG_ID" \
  https://HOST/api/v1/routing/carriers

# is the container's own healthcheck seeing it as healthy
docker inspect -f '{{.State.Health.Status}}' csaas-api-1
```

**Reading `/status`:** `db: down` is the only thing that makes overall `down` - nothing
works without the database. Everything else (redis down, a carrier's breaker open, the
media plane unreachable) degrades the platform without taking the whole thing down, and
shows as `degraded`.

## Log locations

Nothing is written to a host log file by default - everything is `docker logs` /
`docker compose logs` (see above), structured as JSON via structlog. `backup.sh`'s cron
line redirects to `/opt/csaas/backups/backup.log` (see the Backups section) - that is the
one exception, because cron itself has no stdout to capture otherwise.

## Executed results (2026-08-29, Fable-supervised)

- **backup.sh** ran clean on the box after a one-time on-box `sed -i 's/\r$//'`
  (root cause fixed for good: `.gitattributes` now forces `*.sh text eol=lf`, because
  `git archive` from a Windows checkout otherwise ships CRLF and bash rejects
  `set -euo pipefail\r`). Output: `Backup complete:
  /opt/csaas/backups/csaas-20260829-0144.dump (168K)`, perms `-rw------- root`.
- **restore_drill.sh PASSED** end-to-end on the box against that dump: throwaway
  postgres on an isolated drill network, `pg_restore`, `alembic upgrade head` a no-op at
  `0015_calls_supervise`, `smoke_restore.py` all checks passed (orgs/users/roles counts,
  tenant scope both directions — scoped query correctly filtered, unscoped correctly
  refused), containers/network cleaned up. This is the P14 gate's restore half, executed.
- **Bounded VPS load test:** not yet run — read mode needs an operator bearer token
  (see Load testing above). `--self-test` green locally. Run it with your credentials
  and record p50/p95/p99 here.

## Operator step: expose /status and the LiveKit WS through nginx

The public `/status` route and the softphone's LiveKit signal WebSocket (OPEN_ISSUES
I1) both need one addition to the **csaas server block** in
`/etc/nginx/sites-enabled/csaas`. Editing nginx on this shared box was deliberately
left to the operator (the automation's permission layer blocks it, correctly). Steps:

**D11 caveat - do this BEFORE the `ufw deny ... port 7880` rule in "Media plane
bring-up" above, if you haven't applied that rule yet:** if `LIVEKIT_PUBLIC_URL` in
`.env` is currently the direct form (`ws://144.126.152.175:7880` / `ws://<ip>:7880`),
that URL stops working the moment the deny rule is in place - 7880 becomes unreachable
from anywhere but loopback. Add the `location /livekit/` block below and repoint
`LIVEKIT_PUBLIC_URL` to the `wss://<host>/livekit` form FIRST, confirm the softphone
still connects, and only then apply the deny rule. Doing it in the other order takes the
softphone down until this section is completed.

```bash
cp /etc/nginx/sites-enabled/csaas /root/csaas.nginx.bak.$(date +%s)
# insert BEFORE the existing "location /api/ {" block:
```
```nginx
    # Public status page (P14 DR-8): component names + up/degraded/down only.
    location = /status {
        proxy_pass http://127.0.0.1:8080/status;
        proxy_set_header Host $host;
    }

    # LiveKit signal WebSocket for the browser softphone (closes OPEN_ISSUES I1).
    location /livekit/ {
        proxy_pass http://127.0.0.1:7880/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host       $host;
        proxy_set_header X-Real-IP  $remote_addr;
        proxy_read_timeout 600s;
        access_log off;
    }
```
```bash
nginx -t && systemctl reload nginx
# then add to /opt/csaas/.env (by hand, as always) and restart the api:
#   LIVEKIT_PUBLIC_URL=wss://csaas.sabinepropertygroup.net/livekit
curl -s https://csaas.sabinepropertygroup.net/status
```
