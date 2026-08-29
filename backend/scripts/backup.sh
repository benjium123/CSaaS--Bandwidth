#!/usr/bin/env bash
# Nightly Postgres backup (P14 DR-4). Runs ON THE BOX, installed as a root cron line per
# docs/RUNBOOK.md (03:30 CT daily) - deploy.sh deliberately does NOT install cron, since it
# promises to touch nothing outside /opt/csaas.
#
# `docker exec`'s INTO the already-running csaas-db-1 container and pg_dump's over the
# container's own local socket (no network hop, no password prompt) - the dump never
# touches disk inside the container, it streams straight to the host file below.
#
#   ./backup.sh
#
set -euo pipefail

CONTAINER="csaas-db-1"
DB_NAME="csaas"
DB_USER="csaas"
BACKUP_DIR="/opt/csaas/backups"
KEEP=14
STAMP="$(date +%Y%m%d-%H%M)"
OUT="${BACKUP_DIR}/csaas-${STAMP}.dump"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nABORT: %s\n' "$*" >&2; exit 1; }

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" >/dev/null 2>&1; then
  die "${CONTAINER} is not running - is the stack up? (docker compose -f deploy/docker-compose.prod.yml ps)"
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

say "Dumping ${DB_NAME} from ${CONTAINER} -> ${OUT}"
if ! docker exec "$CONTAINER" pg_dump -U "$DB_USER" -Fc "$DB_NAME" > "$OUT"; then
  rm -f -- "$OUT"
  die "pg_dump failed - partial file removed"
fi
chmod 600 "$OUT"

say "Pruning to the newest ${KEEP} dump(s)"
# shellcheck disable=SC2012
ls -1t "${BACKUP_DIR}"/csaas-*.dump 2>/dev/null | tail -n "+$((KEEP + 1))" | while IFS= read -r stale; do
  echo "  removing ${stale}"
  rm -f -- "$stale"
done

say "Backup complete: ${OUT} ($(du -h "$OUT" 2>/dev/null | cut -f1))"
