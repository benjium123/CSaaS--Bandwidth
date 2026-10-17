#!/bin/sh
# Owner 2026-10-17: the Ringlite AI voice-agent worker (csaas-agent-1) stays OFF while no
# workspace has an AI agent, and starts by itself as soon as one exists.
#   - any row in agent_profiles  -> make sure csaas-agent-1 is running (docker start)
#   - no rows                    -> stop it, unless /etc/csaas/agent-always-on exists
# Never `docker compose up`: start/stop reuse the existing container and its settings.
# Runs every minute from csaas-agent-autostart.timer. Logs to the journal (tag csaas-agent-autostart).
set -u
C=csaas-agent-1
TAG=csaas-agent-autostart

n=$(docker exec csaas-db-1 psql -U csaas -d csaas -Atc "select count(*) from agent_profiles" 2>/dev/null)
case "$n" in
  ''|*[!0-9]*) logger -t "$TAG" "could not read agent_profiles (got '$n'); leaving $C as it is"; exit 0 ;;
esac

state=$(docker inspect -f '{{.State.Status}}' "$C" 2>/dev/null) || { logger -t "$TAG" "$C not found"; exit 0; }

if [ "$n" -gt 0 ] && [ "$state" != "running" ]; then
  logger -t "$TAG" "$n AI agent profile(s) exist; starting $C (was $state)"
  docker start "$C" >/dev/null
elif [ "$n" -eq 0 ] && [ "$state" = "running" ] && [ ! -e /etc/csaas/agent-always-on ]; then
  logger -t "$TAG" "no AI agent profiles; stopping $C"
  docker stop "$C" >/dev/null
fi
exit 0
