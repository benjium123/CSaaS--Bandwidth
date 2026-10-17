#!/bin/sh
# Deploy the Ringlite api from a CI-built image. Nothing is built on the box.
#   deploy/deploy-api.sh <git-sha>        (full sha, as tagged by .github/workflows/api-image.yml)
# On the box: pull -> keep the running image as csaas-api:prev -> migrate -> recreate ONLY the
# api (--no-deps --no-build: never touch livekit/agent or their CPU settings).
# Rollback: ssh root@144.126.152.175 'docker tag csaas-api:prev csaas-api:latest && cd /opt/csaas &&
#   docker compose -p csaas --env-file .env -f deploy/docker-compose.prod.yml
#   -f deploy/livekit/docker-compose.livekit.yml up -d --no-deps --no-build api'
# Needs, once: `docker login ghcr.io` on the box with a read:packages token.
set -eu
SHA="${1:?usage: deploy-api.sh <git-sha>}"
TARGET="${TARGET:-root@144.126.152.175}"
IMAGE="ghcr.io/benjium123/csaas-api:${SHA}"

ssh "$TARGET" sh -s "$IMAGE" <<'REMOTE'
set -eu
IMAGE="$1"
cd /opt/csaas
COMPOSE="docker compose -p csaas --env-file .env -f deploy/docker-compose.prod.yml -f deploy/livekit/docker-compose.livekit.yml"
docker pull "$IMAGE"
docker tag "$(docker inspect -f '{{.Image}}' csaas-api-1)" csaas-api:prev
docker tag "$IMAGE" csaas-api:latest
$COMPOSE run --rm --no-deps api alembic upgrade head
$COMPOSE up -d --no-deps --no-build api
for i in $(seq 1 30); do
  s=$(docker inspect -f '{{.State.Health.Status}}' csaas-api-1 2>/dev/null || echo unknown)
  [ "$s" = healthy ] && { echo "api healthy on $IMAGE"; exit 0; }
  sleep 4
done
echo "api NOT healthy after 2 min on $IMAGE - roll back with csaas-api:prev (see header)" >&2
exit 1
REMOTE
