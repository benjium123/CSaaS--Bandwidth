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
PORT=8080

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
echo "pre-flight OK"
REMOTE

say "Shipping tracked files (git archive HEAD)"
git archive --format=tar HEAD | ssh "$TARGET" "tar -x -C ${REMOTE_DIR}"

say "Building and starting (compose project: csaas)"
ssh "$TARGET" "cd ${REMOTE_DIR} && docker compose -f deploy/docker-compose.prod.yml up -d --build"

say "Applying migrations"
ssh "$TARGET" "cd ${REMOTE_DIR} && docker compose -f deploy/docker-compose.prod.yml exec -T api alembic upgrade head"

say "Health check"
ssh "$TARGET" "curl -fsS http://127.0.0.1:${PORT}/healthz" || die "healthz did not come up green"
echo

say "Containers we added"
ssh "$TARGET" "docker ps --filter 'name=csaas' --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'"

say "Deploy complete"
