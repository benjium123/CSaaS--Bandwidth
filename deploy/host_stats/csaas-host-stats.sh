#!/usr/bin/env bash
#
# Write a one-shot `docker stats` snapshot to /opt/csaas/run/host_stats.json so the api
# container (which has no access to the docker socket) can show per-app usage in the ops
# console "Server" page. Run by csaas-host-stats.timer every 15 s.
set -euo pipefail

OUT_DIR="/opt/csaas/run"
OUT_FILE="${OUT_DIR}/host_stats.json"
TMP_FILE="${OUT_DIR}/host_stats.json.tmp"

mkdir -p "${OUT_DIR}"

# docker stats writes one JSON object per line (NDJSON). Write to a temp file in the same
# directory first so the api never reads a half-written file, then move it into place
# atomically. On failure leave the previous good file untouched and exit non-zero.
if ! docker stats --no-stream --format '{{json .}}' > "${TMP_FILE}"; then
    rm -f "${TMP_FILE}"
    echo "csaas-host-stats: docker stats failed; leaving ${OUT_FILE} untouched" >&2
    exit 1
fi

chmod 0644 "${TMP_FILE}"
mv -f "${TMP_FILE}" "${OUT_FILE}"
