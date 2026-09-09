# LiveKit SFU config for the csaas media plane (D17).
# Keys come from LIVEKIT_KEYS in the compose environment - never write them into this file.
port: 7880
bind_addresses:
  - ""
rtc:
  port_range_start: 50700
  port_range_end: 51199
  tcp_port: 7881
  # The VPS has a static public IP; advertising it directly beats STUN round-trips.
  use_external_ip: true
  # Under network_mode: host LiveKit enumerates EVERY host interface, including the
  # 172.x docker bridges of the other tenants' stacks, and advertised all of them as
  # external IPs (seen live 2026-09-09: nine candidates, one real). Dead candidates
  # slow ICE and can be selected first. Pin the advertised node IP and restrict
  # candidate gathering to the public NIC.
  node_ip: 144.126.152.175
  interfaces:
    includes:
      - eth0
redis:
  # Addendum to 8.16: `livekit` runs network_mode: host (deploy/livekit/docker-compose.
  # livekit.yaml), so it no longer sees the compose bridge network's DNS - "redis" would
  # not resolve. Reach the main stack's redis the same way livekit-sip already does via
  # sip.yaml: the loopback port docker-compose.prod.yml publishes it on.
  address: 127.0.0.1:6380
  # D5: this is a .tpl (deploy/livekit/livekit.yaml.tpl), not the literal mount - the
  # literal mount (deploy/livekit/livekit.yaml) is git-ignored and rendered from this
  # file by deploy.sh AFTER every rsync, substituting CSAAS_REDIS_PASSWORD from
  # ${REMOTE_DIR}/.env (see docs/RUNBOOK.md). Hand-pasting the password directly into a
  # git-tracked file used to get silently reverted by the next deploy's
  # `rsync --delete`. Empty CSAAS_REDIS_PASSWORD renders `password: ""`, which livekit
  # treats as no auth - matching main-stack redis's own passwordless-by-default behavior.
  password: "${CSAAS_REDIS_PASSWORD}"
logging:
  level: info
  json: true
room:
  # The backend creates outbound rooms explicitly and the SIP dispatch rule creates
  # inbound ones; nothing legitimate needs implicit creation. With auto_create off, an
  # over-broad or leaked join token cannot be used as a room-creation primitive.
  auto_create: false
# Webhooks: LiveKit posts room/participant lifecycle to the backend, signed with the same
# API key/secret (Authorization: JWT whose sha256 claim hashes the body).
webhook:
  api_key: csaas-media
  urls:
    # Addendum to 8.16: with `livekit` on network_mode: host, its network namespace IS
    # the host's - "api" (the bridge-network service name) no longer resolves, but
    # 127.0.0.1:8080 now correctly reaches the api container, which
    # docker-compose.prod.yml publishes at 127.0.0.1:8080:8080. (Before this change,
    # 127.0.0.1 here would have been the SFU container talking to itself - it was not
    # host-networked yet, so the two 127.0.0.1s were different network namespaces.)
    - http://127.0.0.1:8080/api/v1/webhooks/livekit
