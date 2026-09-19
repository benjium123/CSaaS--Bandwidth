# P43 — Bandwidth live in one week

Drafted 2026-09-19 by DeepSeek V4 Pro from docs/RUNBOOK.md, docs/OPEN_ISSUES.md,
docs/PENDING_OPERATOR_TASKS.md and .env.example; reviewed by Fable (env keys checked
against .env.example, voice webhook paths filled from the router, trunk note aligned with
P42). Depends on P42 (trunk sync) for the LiveKit side. No new APIs are needed - every
Bandwidth surface is already implemented; this is operations plus one small code change.


## 1. What already exists

Bandwidth is already implemented in the CSaaS codebase: messaging adapter, Programmable Voice adapter with BXML, number search/order via the Bandwidth Numbers API, and messaging + voice webhooks with signature/basic-auth checks. The platform has not carried live Bandwidth traffic solely because Bandwidth account `9903389` is a free trial; the voice API returns `402 Outbound call must be to your verified mobile number during your free trial`. The remaining work is account upgrade, Bandwidth portal wiring, `.env` credentials, one code change to make Bandwidth a selectable LiveKit SIP trunk carrier, LiveKit trunk creation, and smoke testing.

## 2. Five-day checklist

### Day 1 — Account upgrade + billing
- [ ] Upgrade Bandwidth account `9903389` from free trial to paid; confirm billing is active.
- [ ] Confirm voice API no longer returns `402` for non-verified outbound numbers.
- [ ] Start/verify Bandwidth 10DLC brand + campaign registration.
- [ ] Confirm `+19404060664` is available/assigned to the account.
- [ ] Confirm firewall ports remain open: UDP `5060`, UDP `50700:51199`, TCP `7881`.

### Day 2 — Bandwidth portal wiring
- [ ] Messaging application callback URL = `https://csaas.sabinepropertygroup.net/api/v1/webhooks/bandwidth/messaging`.
- [ ] Voice application callback URLs: answer = `https://csaas.sabinepropertygroup.net/api/v1/webhooks/bandwidth/voice/answer`, disconnect = `.../webhooks/bandwidth/voice/disconnect`, AMD = `.../webhooks/bandwidth/voice/amd` (routes in `backend/app/api/routes/webhooks.py`).
- [ ] Set same basic-auth username/password on Bandwidth messaging and voice application callbacks as `.env`.
- [ ] Voice application → inbound SIP peer: `144.126.152.175:5060`.
- [ ] Assign `+19404060664` to the voice application and messaging application.
- [ ] Associate the approved 10DLC brand/campaign with the messaging application.

### Day 3 — `.env` and code change
- [ ] Set all `BANDWIDTH_*` keys in `/opt/csaas/.env` per Section 4.
- [ ] Keep `BANDWIDTH_ENABLED=false` until smoke begins; do not make Bandwidth primary.
- [ ] Apply the one code change in Section 3.
- [ ] Restart the API container after `.env`/code deploy.

### Day 4 — LiveKit SIP trunks
- [ ] Create Bandwidth outbound trunk in LiveKit SIP, named `bandwidth-outbound`.
- [ ] Assign outbound number `+19404060664` to the outbound trunk.
- [ ] Create Bandwidth inbound trunk in LiveKit SIP, named `bandwidth-inbound`.
- [ ] Inbound trunk: explicit `numbers` list (P42 keeps it in sync); optionally `allowed_addresses` = Bandwidth's PUBLISHED SIP signalling ranges from the portal (Bandwidth, unlike SignalWire, publishes them). Never an empty `numbers` list: LiveKit's matcher rejects calls when two wildcard trunks both match.
- [ ] Record returned IDs as `LIVEKIT_SIP_BANDWIDTH_TRUNK_ID` and `LIVEKIT_SIP_BANDWIDTH_INBOUND_TRUNK_ID`.
- [ ] Verify SIP signaling from Bandwidth reaches `144.126.152.175:5060`.

### Day 5 — Smoke test
- [ ] Temporarily set `BANDWIDTH_ENABLED=true` for the smoke window.
- [ ] Run the smoke matrix in Section 5.
- [ ] If all pass, keep Bandwidth enabled and decide whether/when to make it primary.
- [ ] If any fail, roll back per Section 7.

## 3. One code change: add `bandwidth` as a trunk carrier

- Add env-backed Settings fields:
  - `LIVEKIT_SIP_BANDWIDTH_TRUNK_ID`
  - `LIVEKIT_SIP_BANDWIDTH_INBOUND_TRUNK_ID`
- Add `"bandwidth"` to `TRUNK_CARRIERS`, e.g.:
  - `TRUNK_CARRIERS = (DEFAULT_TRUNK_CARRIER, "signalwire", "bandwidth")`
- Add a `room_trunks()` entry:
  - `"bandwidth": settings.livekit_sip_bandwidth_trunk_id`
- Include `"bandwidth"` in the P42 trunk-sync carrier list.
- Do not change `DEFAULT_TRUNK_CARRIER`; keep Telnyx as default until Bandwidth is deliberately made primary.

## 4. `BANDWIDTH_*` keys for `/opt/csaas/.env`

| Key | Where the value comes from |
|---|---|
| `BANDWIDTH_ENABLED` | Operations flag; `false` until smoke passes, then `true`. |
| `BANDWIDTH_ACCOUNT_ID` | Bandwidth Dashboard top-right/account id; account `9903389`. |
| `BANDWIDTH_API_USERNAME` | Bandwidth OAuth2 Client ID / API username from Dashboard credentials. |
| `BANDWIDTH_API_PASSWORD` | Bandwidth OAuth2 Client Secret / API password from Dashboard credentials. |
| `BANDWIDTH_AUTH_MODE` | Keep `oauth2` for messaging/voice; current Bandwidth model. |
| `BANDWIDTH_MESSAGING_APPLICATION_ID` | Bandwidth Dashboard → Messaging Applications → application id. |
| `BANDWIDTH_VOICE_APPLICATION_ID` | Bandwidth Dashboard → Voice Applications → application id. |
| `BANDWIDTH_SITE_ID` | Bandwidth Site/sub-account id. |
| `BANDWIDTH_WEBHOOK_USERNAME` | Operator-chosen basic-auth username; same value entered in Bandwidth portal. |
| `BANDWIDTH_WEBHOOK_PASSWORD` | Operator-chosen basic-auth password; same value entered in Bandwidth portal. |
| `BANDWIDTH_DEFAULT_NUMBER` | Default outbound caller id; `+19404060664` if assigned. |
| `BANDWIDTH_MESSAGING_BASE_URL` | Leave default `https://messaging.bandwidth.com/api/v2`. |
| `BANDWIDTH_VOICE_BASE_URL` | Leave default `https://voice.bandwidth.com/api/v2`. |
| `BANDWIDTH_DASHBOARD_BASE_URL` | Leave default `https://dashboard.bandwidth.com/api`. |
| `BANDWIDTH_DASHBOARD_USERNAME` | Dashboard API user for Numbers API; verify after upgrade. |
| `BANDWIDTH_DASHBOARD_PASSWORD` | Dashboard API password for Numbers API; verify after upgrade. |
| `BANDWIDTH_SUBACCOUNT_ID` | Superseded by `BANDWIDTH_SITE_ID`; do not set unless legacy path requires it. |
| `BANDWIDTH_LOCATION_ID` | Bandwidth “SIP Peer” ID in the voice portal. |

## 5. Live smoke test matrix

| Test | Action | Pass criteria |
|---|---|---|
| SMS out | Send SMS from CSaaS via Bandwidth number to test mobile | API accepts; message delivered |
| SMS in | Send SMS to Bandwidth number | Inbound messaging webhook returns 2xx in API logs; message saved |
| MMS out | Send MMS/image from CSaaS via Bandwidth number | API accepts; image delivered |
| MMS in | Send MMS/image to Bandwidth number | Inbound messaging webhook returns 2xx; media saved |
| Voice out | Place outbound call through Bandwidth trunk | Call reaches destination; no `402` free-trial rejection |
| Voice in | Call Bandwidth number | LiveKit inbound trunk routes call; voice webhook returns 2xx |
| Delivery receipts check | `curl -s -H "X-Platform-Ops-Token: $OPS_TOKEN" https://csaas.sabinepropertygroup.net/api/v1/platform/messaging/receipts-check` | Recent `last_receipt_at` for `bandwidth` |
| Inbound webhook logs | Inspect API container logs after each test | 2xx for each Bandwidth messaging/voice webhook |

## 6. Known unverified — OPEN_ISSUES rows mentioning Bandwidth

| ID | What to verify |
|---|---|
| D29 | Spam-class error-code lists in `reputation.py` — Bandwidth `4750-4754` / `4770-4775` sourced from public docs. Validate against real DLRs once Bandwidth traffic is live. |
| D37 | Bandwidth Numbers API uses Basic auth while messaging/voice use OAuth2 client credentials — not verified against live account. Verify one real number search after upgrade; if dashboard API user differs, add separate credential pair. |
| D38 | Bandwidth XML size cap runs on `resp.text`, bounding parse cost not memory. If it ever matters, stream and cap `Content-Length`/bytes read. |
| E2 | No SIP trunk points at the box. Verify Bandwidth portal: voice app → inbound SIP peer `144.126.152.175:5060`, assign `+19404060664`. |
| I3 | `deploy/livekit/README.md` and `sip.yaml` comments document a Telnyx trunk, but voice is now Bandwidth. Doc fix; step 5 must not be followed as written. |
| C3 | R2 unmeasured: VPS ↔ Bandwidth media PoP may be long-haul. Run `measure/` scripts once a live trunk exists. |
| D85 | Failure classification tables taken from carrier docs and unverified. When real DLR traffic exists, compare `failure_class` counts to Bandwidth dashboard for a week and correct tables. |

## 7. Rollback notes

- Keep `BANDWIDTH_ENABLED=false` and do not make Bandwidth primary until smoke passes.
- If smoke fails, revert:
  - Set `BANDWIDTH_ENABLED=false`.
  - Remove or empty `LIVEKIT_SIP_BANDWIDTH_TRUNK_ID` and `LIVEKIT_SIP_BANDWIDTH_INBOUND_TRUNK_ID`.
  - Restart the API container.
- Existing default trunk remains `telnyx`; no calls route through Bandwidth unless explicitly selected.
- Portal wiring may remain, but Bandwidth webhooks/outbound are inert while disabled.
- If voice still returns `402` after upgrade, stop and contact Bandwidth before enabling.
