#!/usr/bin/env bash
# Restore drill (P14 DR-4): prove the newest backup actually restores and serves the app,
# entirely inside THROWAWAY containers.
#
# NEVER touches the live compose project or the live database - the throwaway postgres
# gets its own container name, its own docker network, and tmpfs storage (nothing persists
# past cleanup). This script reads NOTHING from /opt/csaas/.env and touches no live
# secret: the throwaway postgres gets a FRESH, RANDOM, drill-only password generated below
# - it never needs to match anything live, because pg_dump -Fc carries no role passwords
# and the restore runs --no-owner. A live credential is deliberately never read into a
# shell variable or an env file here, so it can never end up in `ps`/`docker inspect`
# output on a box shared with other tenants.
#
#   ./restore_drill.sh [dump_file]
#
# With no argument, restores the newest /opt/csaas/backups/csaas-*.dump. The api image used
# for the alembic-upgrade and smoke-restore steps defaults to "csaas-api" (docker compose
# v2's default tag for the `api` service under the `name: csaas` project in
# deploy/docker-compose.prod.yml) - override with API_IMAGE=... if the box tags it
# differently.
set -euo pipefail

REMOTE_DIR="/opt/csaas"
BACKUP_DIR="${REMOTE_DIR}/backups"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_IMAGE="${API_IMAGE:-csaas-api}"
PG_IMAGE="postgres:16"

DRILL_ID="$$"
NET="csaas-drill-net-${DRILL_ID}"
DB_CONTAINER="csaas-drill-${DRILL_ID}"
DB_USER="csaas"
DB_NAME="csaas"
# Fresh, random, drill-only - never read from .env, never a live credential. Restoring
# with --no-owner means the dump's role ownership is irrelevant to this password.
CSAAS_DB_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -d '/+=')"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nABORT: %s\n' "$*" >&2; exit 1; }

DUMP_FILE="${1:-}"
if [ -z "$DUMP_FILE" ]; then
  # shellcheck disable=SC2012
  DUMP_FILE="$(ls -1t "${BACKUP_DIR}"/csaas-*.dump 2>/dev/null | head -1)"
fi
[ -n "$DUMP_FILE" ] && [ -f "$DUMP_FILE" ] || die "no dump found (looked in ${BACKUP_DIR}, or pass a path)"

cleanup() {
  say "Cleaning up drill containers/network"
  docker rm -f "$DB_CONTAINER" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

say "Dump under test: ${DUMP_FILE}"

say "Creating isolated network ${NET}"
docker network create "$NET" >/dev/null

say "Starting throwaway postgres (${DB_CONTAINER}) - tmpfs data dir, no host port published"
docker run -d --name "$DB_CONTAINER" --network "$NET" \
  --tmpfs /var/lib/postgresql/data \
  -e POSTGRES_USER="$DB_USER" \
  -e POSTGRES_PASSWORD="$CSAAS_DB_PASSWORD" \
  -e POSTGRES_DB="$DB_NAME" \
  "$PG_IMAGE" >/dev/null

say "Waiting for it to accept connections"
ready=0
for _ in $(seq 1 30); do
  if docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[ "$ready" -eq 1 ] || die "throwaway postgres never became ready"
say "Throwaway db reachable at ${DB_CONTAINER}:5432 on the isolated drill network only"

say "Restoring the dump"
docker run --rm --network "$NET" \
  -e PGPASSWORD="$CSAAS_DB_PASSWORD" \
  -v "$(cd "$(dirname "$DUMP_FILE")" && pwd):/dumps:ro" \
  "$PG_IMAGE" \
  pg_restore -h "$DB_CONTAINER" -U "$DB_USER" -d "$DB_NAME" --no-owner --role="$DB_USER" \
    "/dumps/$(basename "$DUMP_FILE")" \
  || die "pg_restore failed"

DRILL_DATABASE_URL="postgresql+asyncpg://${DB_USER}:${CSAAS_DB_PASSWORD}@${DB_CONTAINER}:5432/${DB_NAME}"

say "alembic upgrade head against the restored data (must be a no-op)"
set +e
ALEMBIC_OUT="$(docker run --rm --network "$NET" \
  -e DATABASE_URL="$DRILL_DATABASE_URL" \
  "$API_IMAGE" alembic upgrade head 2>&1)"
ALEMBIC_STATUS=$?
set -e
echo "$ALEMBIC_OUT"
[ "$ALEMBIC_STATUS" -eq 0 ] || die "alembic upgrade head failed"
if echo "$ALEMBIC_OUT" | grep -q "Running upgrade"; then
  die "alembic upgrade head was NOT a no-op - the restored data is not on the schema this image expects"
fi

say "smoke_restore.py against the restored data"
docker run --rm --network "$NET" \
  -e DATABASE_URL="$DRILL_DATABASE_URL" \
  -v "${SCRIPT_DIR}:/app/scripts:ro" \
  "$API_IMAGE" python scripts/smoke_restore.py \
  || die "smoke_restore.py reported a failure - see output above"

say "Restore drill PASSED (${DUMP_FILE})"
