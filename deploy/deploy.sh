#!/usr/bin/env bash
# Deploy CSaaS to the VPS.
#
# THE BOX IS PRODUCTION FOR OTHER BUSINESSES. This script:
#   - touches nothing outside /opt/csaas
#   - never writes, copies or overwrites a .env
#   - ABORTS on any conflict rather than "fixing" it
#   - is idempotent; re-running is safe
#
# Usage: ./deploy/deploy.sh [user@host]
set -euo pipefail

TARGET="${1:-root@144.126.152.175}"
REMOTE_DIR="/opt/csaas"
STAGING_DIR="${REMOTE_DIR}/.deploy-staging"
# --env-file is REQUIRED: `${VAR}` interpolation in the compose file resolves against the
# compose file's own directory (deploy/), not against the service-level `env_file:`. Without
# it the first deploy dies on "CSAAS_DB_PASSWORD is missing a value".
PORT=8080
COMPOSE_MAIN="deploy/docker-compose.prod.yml"
COMPOSE_LIVEKIT="deploy/livekit/docker-compose.livekit.yml"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mABORT: %s\033[0m\n' "$*" >&2; exit 1; }

say "Pre-flight checks on ${TARGET}"

ssh "$TARGET" bash -s <<REMOTE || die "pre-flight failed - nothing was changed"
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is not installed."
  echo "This box runs production services; refusing to install packages automatically."
  echo "Install Docker manually, then re-run this script."
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose v2 plugin is missing. Install it manually, then re-run."
  exit 1
fi

mkdir -p "${REMOTE_DIR}"

# Port must be free, or already held by our own container.
if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  owner=\$(docker ps --filter "publish=${PORT}" --format '{{.Names}}' | head -1 || true)
  if [ -z "\$owner" ] || ! echo "\$owner" | grep -q '^csaas'; then
    echo "Port ${PORT} is in use by something that is not ours (\${owner:-unknown})."
    exit 1
  fi
fi

if [ ! -f "${REMOTE_DIR}/.env" ]; then
  echo ""
  echo "MISSING: ${REMOTE_DIR}/.env"
  echo "Create it by hand from .env.example, then re-run. This script never"
  echo "transfers secrets and never overwrites an existing .env."
  exit 1
fi

# 8.22: a world/group-readable .env on a box shared with other tenants leaks every
# carrier/LLM/DB credential in it. Refuse rather than silently proceeding.
env_mode="\$(stat -c '%a' "${REMOTE_DIR}/.env" 2>/dev/null || echo unknown)"
if [ "\$env_mode" != "600" ]; then
  echo ""
  echo "REFUSING: ${REMOTE_DIR}/.env has mode \${env_mode} (expected 600)."
  echo "Fix it: chmod 600 ${REMOTE_DIR}/.env"
  exit 1
fi

# D10: catch a missing required secret HERE, before migrations run - previously
# CSAAS_DB_PASSWORD/LIVEKIT_API_SECRET only surfaced as compose's `:?` interpolation
# error partway through "Applying migrations"/"Building and starting the full stack"
# (both required - migrations need CSAAS_DB_PASSWORD via DATABASE_URL, the livekit
# stack needs LIVEKIT_API_SECRET via LIVEKIT_KEYS), by which point db is already up and
# possibly already migrated.
for var in CSAAS_DB_PASSWORD LIVEKIT_API_SECRET; do
  if ! grep -Eq "^\${var}=.+" "${REMOTE_DIR}/.env"; then
    echo ""
    echo "MISSING/EMPTY: \${var} in ${REMOTE_DIR}/.env"
    echo "Set it, then re-run. (Checked now so a missing var cannot fail AFTER migrations ran.)"
    exit 1
  fi
done

echo "pre-flight OK"
REMOTE

# 8.5: build the console from a CLEAN git export, not the local working tree, so the
# shipped dist is reproducible from what's actually in git. ALLOW_DIRTY_FRONTEND=1 is an
# explicit, named escape hatch for local iteration - it is not the default path.
FRONTEND_DIST=""
if [ -d frontend ]; then
  DIRTY="$(git status --porcelain -- frontend/ 2>/dev/null || true)"
  if [ -n "$DIRTY" ] && [ "${ALLOW_DIRTY_FRONTEND:-0}" != "1" ]; then
    die "frontend/ has uncommitted changes - the build would not match what's in git:
${DIRTY}
Commit or stash them, then re-run. Or set ALLOW_DIRTY_FRONTEND=1 to build the dirty
working tree anyway (NOT reproducible - do not use this for a real release)."
  fi

  if [ -n "$DIRTY" ]; then
    say "ALLOW_DIRTY_FRONTEND=1: building the LOCAL working tree (not a clean git export)"
    BUILD_DIR="frontend"
  else
    say "Building the console from a clean git export (node stays off the production box)"
    BUILD_TMP="$(mktemp -d)"
    trap 'rm -rf "$BUILD_TMP"' EXIT
    git archive HEAD frontend | tar -x -C "$BUILD_TMP"
    BUILD_DIR="$BUILD_TMP/frontend"
  fi

  ( cd "$BUILD_DIR" && npm ci --no-audit --no-fund && npm run build ) || die "frontend build failed"
  FRONTEND_DIST="$BUILD_DIR/dist"
else
  echo "no frontend/ directory - skipping"
fi

# 8.8: stage the tracked-file archive, then rsync --delete it into place so files removed
# from git actually disappear from the box (a plain tar overlay never deletes stale
# files). .env, backups/, var/ (media volume mount point) and frontend/dist (shipped
# separately, immediately below) are excluded so this can never touch them.
say "Shipping tracked files (git archive HEAD) via a staging dir"
ssh "$TARGET" "mkdir -p ${STAGING_DIR} && rm -rf ${STAGING_DIR:?}/* ${STAGING_DIR}/.[!.]* 2>/dev/null; mkdir -p ${STAGING_DIR}"
git archive --format=tar HEAD | ssh "$TARGET" "tar -x -C ${STAGING_DIR}"
ssh "$TARGET" "rsync -a --delete \
  --exclude='.env' --exclude='backups/' --exclude='var/' --exclude='frontend/dist/' \
  --exclude='.deploy-staging/' \
  --exclude='deploy/livekit/livekit.yaml' --exclude='deploy/livekit/sip.yaml' \
  ${STAGING_DIR}/ ${REMOTE_DIR}/"
ssh "$TARGET" "rm -rf ${STAGING_DIR}"

# D5: livekit.yaml/sip.yaml carry the redis password and are git-ignored (see
# deploy/livekit/README.md + docs/RUNBOOK.md) - they are rendered from their .tpl
# counterparts HERE, AFTER the rsync above, so a rendered file is never mistaken for
# tracked content and never wiped by the next deploy's `rsync --delete` (the excludes
# just above are what keep --delete from touching them in the first place). Reads
# CSAAS_REDIS_PASSWORD from the box's own .env, never from this script's environment -
# an unset/empty value renders `password: ""`, which livekit treats as no auth, matching
# main-stack redis's own passwordless-by-default behavior.
say "Rendering livekit/sip config from .tpl (CSAAS_REDIS_PASSWORD from ${REMOTE_DIR}/.env)"
ssh "$TARGET" bash -s <<REMOTE || die "rendering livekit/sip config failed"
set -euo pipefail
if ! command -v envsubst >/dev/null 2>&1; then
  echo "envsubst is not installed (gettext-base). Install it manually, then re-run."
  exit 1
fi
# Pull just this one value out of .env rather than sourcing the whole file (.env is
# operator-edited free text, not something this script should execute). `|| true`
# because CSAAS_REDIS_PASSWORD is optional (unset -> grep finds nothing -> exit 1, which
# `set -e -o pipefail` would otherwise treat as this whole step failing).
CSAAS_REDIS_PASSWORD="\$( (grep -E '^CSAAS_REDIS_PASSWORD=' "${REMOTE_DIR}/.env" || true) | tail -n1 | cut -d= -f2- | sed -e 's/[[:space:]]*#.*\$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*\$//')"
export CSAAS_REDIS_PASSWORD
envsubst '\${CSAAS_REDIS_PASSWORD}' < "${REMOTE_DIR}/deploy/livekit/livekit.yaml.tpl" > "${REMOTE_DIR}/deploy/livekit/livekit.yaml"
envsubst '\${CSAAS_REDIS_PASSWORD}' < "${REMOTE_DIR}/deploy/livekit/sip.yaml.tpl" > "${REMOTE_DIR}/deploy/livekit/sip.yaml"
REMOTE

# frontend/dist is a build artifact, gitignored (not in the archive above) and NOT
# touched by the rsync excludes - replace it explicitly: rm -rf then extract the fresh
# build (8.8).
if [ -n "$FRONTEND_DIST" ] && [ -d "$FRONTEND_DIST" ]; then
  say "Replacing the built console (frontend/dist)"
  ssh "$TARGET" "rm -rf ${REMOTE_DIR}/frontend/dist && mkdir -p ${REMOTE_DIR}/frontend/dist"
  tar -cf - -C "$FRONTEND_DIST" . | ssh "$TARGET" "tar -x -C ${REMOTE_DIR}/frontend/dist"
fi

# 8.4: bring the database up first and wait for it to actually be healthy before
# anything tries to talk to it (migrations included).
say "Starting the database (compose project: csaas)"
ssh "$TARGET" "cd ${REMOTE_DIR} && docker compose --env-file .env -f ${COMPOSE_MAIN} up -d db"

say "Waiting for db to report healthy"
ssh "$TARGET" bash -s <<REMOTE || die "db did not become healthy"
set -euo pipefail
status=""
for i in \$(seq 1 30); do
  status="\$(docker inspect -f '{{.State.Health.Status}}' csaas-db-1 2>/dev/null || echo '')"
  if [ "\$status" = "healthy" ]; then
    echo "db is healthy"
    exit 0
  fi
  sleep 2
done
echo "db did not become healthy within ~60s (last status: \${status:-unknown})"
exit 1
REMOTE

# 8.4: migrations run against a freshly built image, BEFORE the api container starts
# serving traffic on the old schema (or, worse, the new code against an old schema).
say "Building the api image"
ssh "$TARGET" "cd ${REMOTE_DIR} && docker compose --env-file .env -f ${COMPOSE_MAIN} build api"

say "Applying migrations"
ssh "$TARGET" "cd ${REMOTE_DIR} && docker compose --env-file .env -f ${COMPOSE_MAIN} run --rm --no-deps api alembic upgrade head"

# 8.9: include the LiveKit media-plane compose file so a normal deploy actually keeps it
# up to date - previously it was never applied here and drifted from what deploy.sh
# shipped. Requires LIVEKIT_API_SECRET already set in .env (see deploy/livekit/README.md);
# if the media plane has not been brought up yet on this box, do that first (RUNBOOK.md,
# "Media plane bring-up (B3)") or this step will fail on the required-var check.
say "Building and starting the full stack (compose project: csaas)"
ssh "$TARGET" "cd ${REMOTE_DIR} && docker compose --env-file .env -f ${COMPOSE_MAIN} -f ${COMPOSE_LIVEKIT} up -d --build"

# 8.19: the api container can take a few seconds to bind after `up -d` returns - retry
# instead of failing on the first miss.
say "Health check"
ssh "$TARGET" bash -s <<REMOTE || die "healthz did not come up green after ~30s"
set -euo pipefail
for i in \$(seq 1 15); do
  if curl -fsS http://127.0.0.1:${PORT}/healthz >/dev/null 2>&1; then
    echo "healthz OK"
    exit 0
  fi
  sleep 2
done
echo "healthz did not come up green"
exit 1
REMOTE

say "Containers we added"
ssh "$TARGET" "docker ps --filter 'name=csaas' --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"

say "Deploy complete"
