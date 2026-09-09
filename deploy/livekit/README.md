# LiveKit media plane — bring-up

One media plane for the browser softphone (P6) and the AI agent (P7+) — decision D17.
PSTN reaches LiveKit through a **Telnyx SIP trunk** ↔ `livekit-sip` ↔ SFU rooms.

## One-time setup

### 1. Secrets
```bash
# in /root/csaas/.env on the VPS
LIVEKIT_API_SECRET=$(openssl rand -hex 32)   # key id is fixed: csaas-media
```

### 1b. Redis password (D5)
`deploy/livekit/livekit.yaml` and `deploy/livekit/sip.yaml` are **generated**, git-ignored
files — never edit or commit them. Their source is `livekit.yaml.tpl` / `sip.yaml.tpl`
(git-tracked, containing `password: "${CSAAS_REDIS_PASSWORD}"`); `deploy.sh` renders the
`.tpl` → `.yaml` with `envsubst` on the box, AFTER every rsync, reading
`CSAAS_REDIS_PASSWORD` from `/opt/csaas/.env` (empty is fine — renders `password: ""`,
which LiveKit treats as no auth, matching main-stack redis's own passwordless-by-default
behavior). This is why: a real password hand-pasted directly into the old git-tracked
`.yaml` files used to get silently reverted by the next deploy's `rsync --delete`.

Local dev / rendering by hand (e.g. testing the compose file outside `deploy.sh`):
```bash
CSAAS_REDIS_PASSWORD=whatever-your-.env-has envsubst '${CSAAS_REDIS_PASSWORD}' \
  < deploy/livekit/livekit.yaml.tpl > deploy/livekit/livekit.yaml
CSAAS_REDIS_PASSWORD=whatever-your-.env-has envsubst '${CSAAS_REDIS_PASSWORD}' \
  < deploy/livekit/sip.yaml.tpl > deploy/livekit/sip.yaml
```

### 2. Telnyx SIP trunk (user does this in the Telnyx portal — wall-clock dependency)
1. **Voice → SIP Trunking → Create SIP Connection**, type **FQDN**:
   FQDN = the VPS IP `144.126.152.175`, port `5060`, transport UDP.
   Enable outbound; set a Credentials username/password (note them).
2. Assign the voice phone number(s) to this SIP Connection.
3. **Outbound Voice Profile**: create one, attach the SIP Connection, allowed
   destinations US/CA.

### 3. Firewall (ufw)
```bash
ufw allow 7881/tcp
ufw allow 50700:51199/udp
ufw allow 5060/udp        # tighten to Telnyx signaling ranges once verified
ufw allow 10000:10499/udp
ufw deny in on <public-iface> to any port 7880 proto tcp
```
D11: 7880 does **not** stay loopback — `livekit` runs `network_mode: host` (see the
compose file's own comments), so `bind_addresses: [""]` in `livekit.yaml` means 7880
listens on every interface the host has, public IP included, unless the `deny` rule
above blocks it. Docker's usual `127.0.0.1:PORT:PORT` publish mapping does not exist
under host networking — there is no docker-proxy to restrict this port, only ufw.
nginx's `proxy_pass` to 127.0.0.1:7880 is unaffected by a deny rule scoped to the public
interface. If the softphone needs to reach LiveKit publicly, add the `wss://…/livekit`
location block to `deploy/nginx-csaas.conf` and repoint `LIVEKIT_PUBLIC_URL` to that
`wss://` URL **before** applying the deny rule above — see docs/RUNBOOK.md.

### 4. Start
```bash
docker compose -f deploy/docker-compose.prod.yml \
               -f deploy/livekit/docker-compose.livekit.yml up -d
```

### 5. Trunks + dispatch rule inside LiveKit (once, via the lk CLI)
```bash
# inbound: any call arriving on the trunk lands in room call-<callID>
lk sip inbound create --url ws://127.0.0.1:7880 --api-key csaas-media --api-secret $LIVEKIT_API_SECRET \
  '{"name":"telnyx-in","numbers":["+1XXXXXXXXXX"]}'
lk sip dispatch create ... '{"rule":{"dispatchRuleIndividual":{"roomPrefix":"call-"}}}'

# outbound: how livekit-sip reaches Telnyx
lk sip outbound create ... '{"name":"telnyx-out","address":"sip.telnyx.com","numbers":["+1XXXXXXXXXX"],"authUsername":"<trunk user>","authPassword":"<trunk pass>"}'
# → put the returned trunk id into .env as LIVEKIT_SIP_OUTBOUND_TRUNK_ID
```

## Sanity checks
- `docker logs csaas-livekit-1` shows `starting LiveKit server` with the key loaded.
- `lk room list` (same key/secret) answers.
- After trunk setup: dial the number from a cell phone → `lk room list` shows a
  `call-…` room with one SIP participant. That is P6's "inbound reaches a room" gate.

## Do-not (learned elsewhere, applies here)
- `livekit-sip` must stay `network_mode: host`. Docker bridge NAT rewrites SDP wrong
  and produces one-way audio.
- Do not widen the RTP ranges casually — this VPS runs other tenants.
