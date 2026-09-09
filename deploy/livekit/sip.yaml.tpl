# livekit-sip config (host network). Bridges Telnyx SIP trunks <-> LiveKit rooms.
api_key: csaas-media
# api_secret comes from the LIVEKIT_API_SECRET environment variable.
ws_url: ws://127.0.0.1:7880
redis:
  address: 127.0.0.1:6380   # main stack's redis published on loopback for host-net services
  # D5: this is a .tpl (deploy/livekit/sip.yaml.tpl), not the literal mount - the literal
  # mount (deploy/livekit/sip.yaml) is git-ignored and rendered from this file by
  # deploy.sh AFTER every rsync, substituting CSAAS_REDIS_PASSWORD from
  # ${REMOTE_DIR}/.env (see docs/RUNBOOK.md). Empty CSAAS_REDIS_PASSWORD renders
  # `password: ""`, which livekit treats as no auth - matching main-stack redis's own
  # passwordless-by-default behavior.
  password: "${CSAAS_REDIS_PASSWORD}"
sip_port: 5060
rtp_port:
  start: 10000
  end: 10499
logging:
  level: info
  json: true
