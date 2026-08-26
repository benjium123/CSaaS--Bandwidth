# LiveKit media plane — bring-up

One media plane for the browser softphone (P6) and the AI agent (P7+) — decision D17.
PSTN reaches LiveKit through a **Telnyx SIP trunk** ↔ `livekit-sip` ↔ SFU rooms.

## One-time setup

### 1. Secrets
```bash
# in /root/csaas/.env on the VPS
LIVEKIT_API_SECRET=$(openssl rand -hex 32)   # key id is fixed: csaas-media
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
```
7880 stays loopback; nginx terminates TLS for the browser (`wss://…/livekit`) and
proxies to 127.0.0.1:7880 (add the location block to deploy/nginx-csaas.conf when
enabling the softphone publicly).

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
