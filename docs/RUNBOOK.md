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

```bash
./deploy/deploy.sh [user@host]     # defaults to root@144.126.152.175
```

Pre-flight checked, idempotent, safe to re-run. It builds the console locally (node never
runs on the box), ships tracked files via `git archive`, brings the stack up with
`docker compose ... up -d --build`, runs `alembic upgrade head` inside the api container,
and checks `/healthz`. It NEVER writes `.env` - if `/opt/csaas/.env` is missing, it aborts
and tells you to create it by hand from `.env.example`.

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
crontab -e
# add (03:30 America/Chicago - adjust for the box's actual TZ/DST if it is not already CT):
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
cd /opt/csaas && docker compose --env-file .env \
  -f deploy/docker-compose.prod.yml \
  -f deploy/livekit/docker-compose.livekit.yml up -d livekit livekit-sip
```

Verify the target ports are free (nothing listening on 7880/7881/5060) BEFORE running this
- the RTP ranges above are deliberately narrow because the VPS hosts other tenants' services
alongside csaas.

`deploy/livekit/README.md` documents the trunk setup as **Telnyx**; with the Bandwidth/
Telnyx split (B1), voice is on **Bandwidth**, so the inbound trunk step takes Bandwidth's
signaling hosts, not `sip.telnyx.com`.

Residual items as of the last time this ran (check `docs/PROGRESS.md` for current state):
nginx needs a `wss` proxy location for port 7880 before the browser softphone can connect
(additive change to the shared csaas nginx site - get authorization before touching a
config file nginx shares with other tenants), and the AI agent needs its own worker
service/venv on the box (the `agents/` code is shipped; nothing runs it yet).

---

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
