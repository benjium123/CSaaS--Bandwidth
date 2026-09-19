# P42 — LiveKit trunk number sync (numbers never require trunk surgery again)

Written 2026-09-19 by Fable after the SignalWire number purchase showed the problem.

## Problem (measured)

- LiveKit SIP trunks carry an explicit `numbers` list. Ten SignalWire numbers were bought
  tonight and could not take or place calls until the trunks were rebuilt, because the
  running `livekit/livekit-server:v1.8` (image built 2025-03-02, reports v1.8.4) has no
  `UpdateSIPInboundTrunk` / `UpdateSIPOutboundTrunk` RPC (`bad_route`). The only edit is
  delete + recreate, which changes trunk ids (the outbound id lives in `.env`) and needs
  the dispatch rule recreated too. That cannot be the process every time we or a client
  buy a number.
- The "wildcard trunk per carrier" alternative was evaluated and REJECTED: LiveKit's
  matcher (`livekit/protocol` `sip.MatchTrunkDetailed`) errors with
  "multiple default trunks" whenever more than one empty-`numbers` trunk passes the
  address filter, and SignalWire sends INVITEs from rotating DigitalOcean/Vultr/AWS
  addresses (14 A records on `sip.signalwire.com` tonight; SignalWire documents that it
  publishes no static list), so a SignalWire wildcard trunk cannot be fenced by
  `allowed_addresses`, and any Telnyx INVITE would then match both wildcard trunks.
  Digest auth on the inbound trunk does not help: auth is checked AFTER trunk selection.

## Decision

1. **Upgrade `livekit-server` to the current 1.13 line** (`livekit/livekit-server:v1.13.7`,
   released 2026-09-14; `livekit/sip:v1.8` image is already from 2026-07-23 and stays).
   The Update RPCs exist there, with `numbers: {add|set|remove}` partial updates
   (docs.livekit.io telephony trunk pages). Operator step: change the image tag in
   `deploy/livekit/docker-compose.livekit.yml`, `docker compose pull livekit`, restart
   `livekit` then `livekit-sip`; verify inbound + outbound on the 469 and a 972 afterwards.
2. **Backend owns trunk membership.** New module `app/voice_plane/trunk_sync.py`:
   - `trunk_ids_for(carrier, settings) -> (inbound_id, outbound_id)` from NEW settings
     `LIVEKIT_SIP_TELNYX_INBOUND_TRUNK_ID`, `LIVEKIT_SIP_SIGNALWIRE_INBOUND_TRUNK_ID`
     (outbound ids already exist: `livekit_sip_outbound_trunk_id` = Telnyx,
     `livekit_sip_signalwire_trunk_id`). Empty setting = carrier has no trunk = no-op.
   - `ensure_number(lk, settings, carrier, e164)` → `UpdateSIPInboundTrunk`
     `{"sip_trunk_id": id, "update": {"numbers": {"add": [e164]}}}` and the outbound
     twin. Idempotent (adding a present number is a no-op on LiveKit's side; the code
     still reads the trunk first and skips when present so logs stay honest).
   - `remove_number(...)` mirrors it with `remove`.
   - `reconcile(session, lk, settings)` → for each carrier with trunk ids: set inbound AND
     outbound `numbers` to exactly the ACTIVE `org_numbers` e164s of that carrier
     (`{"numbers": {"set": [...]}}`). This is the self-healing path and also what fixes
     the box on first deploy. It NEVER touches trunks whose id is not in settings (the
     CRM's own LiveKit is a separate instance; `telnyx-in` ST_saRGfbxhRubz on ours is
     the CRM's shared-agent trunk and is not ours to edit).
   - Every call is wrapped: `LiveKitApiError` with `bad_route` (old server) or transport
     failure → `log.warning("trunk_sync_unavailable", ...)` and return False. Provisioning
     must never fail or roll back because trunk sync failed; the sweeper reconcile heals it.
3. **Hooks** (each a 3-5 line call into the module):
   - `services/number_orders.py`: when an order settles to `status = "active"`.
   - `api/routes/numbers.py`: `add_number` (import of an already-owned number) after the
     row is committed; `release` after the row is marked released.
   - `services/sweeper` (the existing periodic pass that logs `sweeper_pass`): call
     `reconcile` at most once per 10 minutes, counter `trunk_numbers_synced` in the log line.
4. **Outbound trunks keep their number lists** (LiveKit accepts an empty list because
   `CreateSIPParticipant` already passes `sip_number` = the from number, but an explicit
   list is a cheap allow-list against a from-number typo dialling out on the wrong
   carrier's credentials). Reconcile sets both.

## Allowed files (implementer)

- `backend/app/voice_plane/trunk_sync.py` (new)
- `backend/app/voice_plane/livekit_api.py` — ONLY to add thin `update_sip_inbound_trunk`,
  `update_sip_outbound_trunk`, `get_sip_inbound_trunk`, `get_sip_outbound_trunk`
  wrappers around the existing `_twirp`; no other change.
- `backend/app/config.py` — the two new settings + the `_env_names` style mapping if the
  file has one for LiveKit (mirror how `livekit_sip_signalwire_trunk_id` is listed).
- `backend/app/services/number_orders.py`, `backend/app/api/routes/numbers.py`,
  `backend/app/services/sweeper.py` (or wherever `sweeper_pass` is emitted — find it, do
  not guess) — hook calls only.
- `backend/tests/test_p42_trunk_sync.py` (new)
- `.env.example` — the two new keys with a one-line comment each.
- `deploy/livekit/docker-compose.livekit.yml` — image tag `livekit/livekit-server:v1.13.7`
  with a comment dated 2026-09-19 saying why (Update RPCs).
- `docs/RUNBOOK.md` — one short section "Trunk numbers are synced by the backend".

## Forbidden

- Any migration or model change (no DB schema; `org_numbers` is read only).
- `deploy/docker-compose.prod.yml`, `.env`, anything under `deploy/livekit/*.yaml`,
  `deploy/signalwire_voice_setup.py`, `deploy/livekit/sip.yaml`.
- Changing `CreateSIPParticipant` / `room_trunks` / call placement.
- Any file not listed above. Max 10 files.

## Test spec

Unit (all with a fake `LiveKitApi` whose `_twirp` records calls and can raise):
- [ ] `ensure_number` adds to BOTH trunks with `numbers.add` when the number is absent.
- [ ] `ensure_number` makes no update call when the number is already present.
- [ ] `ensure_number` is a silent no-op (returns False, no raise) when the carrier has no
      configured trunk id.
- [ ] `ensure_number` returns False and logs `trunk_sync_unavailable` when `_twirp`
      raises `LiveKitApiError` with `bad_route` in the message; nothing else raised.
- [ ] `remove_number` sends `numbers.remove`.
- [ ] `reconcile` sends `numbers.set` = sorted active e164s per carrier, skips released /
      inactive rows, and never sends anything for a carrier without trunk ids.
- [ ] number order poll: an order settling to active triggers `ensure_number` once with
      the row's carrier and e164 (patch the module function).
- [ ] `release` route triggers `remove_number`; a failing `remove_number` does not turn
      the release into an error response.

Manual (operator, after image upgrade + deploy):
- [ ] `docker exec csaas-api-1 python -c` reconcile once; `ListSIPInboundTrunk` shows
      signalwire-in = active SignalWire org_numbers, telnyx-csaas-in = the two 972s.
- [ ] Inbound call to the 469 and to a 972 still land (livekit-sip "SIP participant
      joined room"); outbound from each still completes.

Pass criteria: all unit tests green, `pytest backend/tests -q` no new failures.

## Deploy

yes — `bash deploy/deploy.sh` after the operator has switched the livekit image and
restarted it. Commit message: `feat(p42): backend keeps LiveKit trunk numbers in sync`.
