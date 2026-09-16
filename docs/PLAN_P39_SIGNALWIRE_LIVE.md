# P39 — SignalWire numbers live in CSaaS

Written 2026-09-16. Operator has two SMS+voice-capable numbers in the SignalWire space
`sabine.signalwire.com` and wants them usable in CSaaS:

| Number | SignalWire name | SignalWire number id |
|---|---|---|
| +1 682 423 1003 | Ben Ethan | `08ede4ed-ab9a-40bc-a94b-45fe39459a0c` |
| +1 469 210 3654 | (none) | `b724ed20-2ba7-4e34-aaee-44a97ecbf169` |

## What "live" means here, honestly
- **Texting: fully live** after this phase - inbound texts land in the inbox, outbound texts
  go out from either number, delivery receipts arrive, allowance/prepaid metering applies.
- **Calling: NOT live on these numbers yet.** The softphone and the AI both live in LiveKit
  rooms, and PSTN reaches LiveKit only through the Telnyx SIP trunk. SignalWire has no trunk
  into LiveKit, and whether SignalWire can even place OUTBOUND calls from an external SIP
  server is undocumented (verified 2026-09-16: inbound via their "Domain Apps"/BYOC is
  documented; outbound is not; LiveKit documents only Telnyx). Until a spike proves it, a
  call arriving on a SignalWire number gets the platform's existing "not yet configured for
  inbound calls" announcement and hangs up (`routes/webhooks.py:130-134`). Setting the voice
  webhook now is still right: the call is logged, verified, and ready for the trunk phase.
  The trunk spike is its own slice (P40), not part of this.

## Verified facts this plan rests on (2026-09-16)
- Box is at migration `0040`. Commits `53962d4`, `6bd8856`, `56925d3` (prepaid gate, managed
  telephony, package allowances + the SignalWire **voice** adapter and registry wiring) are
  **not deployed**. `deploy.sh` runs `alembic upgrade head`, so one deploy ships all of it.
- SignalWire messaging adapter, number provider (`lookup_owned_number`) and webhook parser
  already exist on the box (P17/P18). No SignalWire env is set there (all empty).
- Webhook routes are generic: `/api/v1/webhooks/signalwire/messaging` and
  `/api/v1/webhooks/signalwire/voice` exist with no code change.
- Texting webhooks verify against **global** `SIGNALWIRE_WEBHOOK_URL`; voice webhooks resolve
  the carrier from the **global** registry only. So credentials go in `.env` (global), not
  the per-workspace Providers page. The signing URL must be byte-identical to what
  SignalWire calls (no query string - D81 applies to messaging too).
- One workspace: **Sabine Property Group** (`a518b97a-…`). Existing numbers: one Bandwidth,
  one Telnyx (per-workspace account "Sabine Telnyx").
- **10DLC is a hard gate at SignalWire**: "you will not be able to send messages from a
  local US number to another local US number" without an approved Campaign Registry
  campaign with the numbers assigned to it. Capability icons in the dashboard are not the
  campaign. If the campaign is still pending, inbound works and outbound is REJECTED by
  SignalWire (our adapter reports it as a carrier rejection, nothing is lost silently).

## Two code gaps found while checking, fixed in this phase
1. **"Add number" cannot add a SignalWire number from the UI.** The Numbers page posts only
   the e164; the backend then defaults the carrier to the deployment primary (Bandwidth) and
   asks Bandwidth whether it owns +1 682…, which it does not → rejected. Fix: when the caller
   names no carrier, ask every ownership-capable carrier in the registry which one owns the
   number and record THAT one. Ownership is still verified; nothing is trusted on say-so;
   the UI stays as it is (no new field - "do not clutter it").
2. **SignalWire outbound texts never get delivery receipts.** `send_message` does not send
   `StatusCallback`, although the webhook parser already maps `MessageStatus`
   delivered/undelivered/failed to our events (`signalwire/webhooks.py:30-38`). A SignalWire
   text shows "sent" forever. Fix: send `StatusCallback=<SIGNALWIRE_WEBHOOK_URL>` on every
   send when the URL is configured. Logged as **D83**.

## Allowed files (implementer)
- `backend/app/api/routes/numbers.py` — `add_number` only: owner-carrier resolution when
  `payload.carrier is None`. Behaviour when a carrier IS named stays exactly as today.
- `backend/app/providers/signalwire/adapter.py` — `send_message`: add the `StatusCallback`
  form pair when `self._webhook_url` is set.
- `backend/tests/test_signalwire_messaging.py` — NEW (there is no messaging test file for
  SignalWire today; only `test_signalwire_voice.py`).
- `backend/tests/` — the existing add-number tests (implementer locates them; likely in
  `test_p18_number_provisioning.py`) gain the auto-detect cases.
- `deploy/signalwire_number_webhooks.sh` — NEW ops script (below).
- `docs/RUNBOOK.md` (go-live section), `docs/OPEN_ISSUES.md` (D83), `docs/ROADMAP.md`.

## Forbidden
- `.env` / secrets (operator), `backend/migrations/**`, `backend/app/services/**`,
  the frontend, nginx, anything outside the list above.

## Implementation notes

### add_number owner resolution (`routes/numbers.py`)
```python
if payload.carrier is None:
    providers = [n for n in registry.names()
                 if isinstance(registry.get(n), numbers_api.NumberProvider)]
    owners = {}
    for name in providers:
        try:
            owners[name] = await registry.get(name).lookup_owned_number(normalized)
        except Exception:
            owners[name] = None
    owned_by = [n for n, v in owners.items() if v is True]
    if len(owned_by) == 1:
        carrier_name = owned_by[0]
    elif len(owned_by) > 1:
        raise ValidationFailedError("more than one provider claims this number; name the carrier")
    elif providers and all(v is False for v in owners.values()):
        raise ValidationFailedError("number is not owned by any configured provider")
    # else: fall through to the existing primary-carrier path and its existing
    # None-means-unverified handling, so a deployment with no verifiable carrier keeps
    # today's manual-add behaviour unchanged.
```
Then the existing per-carrier ownership block runs unchanged against the resolved carrier.
Keep it in `add_number`; do not touch `order_number` (P18) or the provisioning services.

### StatusCallback (`signalwire/adapter.py`)
One pair appended after `Body`: `("StatusCallback", self._webhook_url)` guarded by
`if self._webhook_url:`. No query string (signature). `capabilities.sync_delivery_status`
stays True - the create response still carries the initial status; the callback adds the
final one.

### Ops script (`deploy/signalwire_number_webhooks.sh`)
Runs ON THE BOX, reads `/opt/csaas/.env`, never takes the token as an argument, prints
nothing secret. For each number id: `POST
https://$SIGNALWIRE_SPACE_URL/api/laml/2010-04-01/Accounts/$SIGNALWIRE_PROJECT_ID/IncomingPhoneNumbers/<id>.json`
with Basic auth `$SIGNALWIRE_PROJECT_ID:$SIGNALWIRE_API_TOKEN` and form fields:
`SmsUrl=$PUBLIC_BASE_URL/api/v1/webhooks/signalwire/messaging`, `SmsMethod=POST`,
`VoiceUrl=$PUBLIC_BASE_URL/api/v1/webhooks/signalwire/voice`, `VoiceMethod=POST`,
`StatusCallback=$PUBLIC_BASE_URL/api/v1/webhooks/signalwire/voice`,
`StatusCallbackMethod=POST`. Idempotent (re-running sets the same values). Prints the
response's `sms_url`/`voice_url` so the operator can read back what SignalWire stored.
Verified endpoint + parameter names (SignalWire compatibility API docs, 2026-09-16).
**Unverified:** whether this also flips the dashboard's "Handle messages/calls using" to
"LaML Webhooks" - the operator checks each number's page after running it.

## Operator steps (wall-clock, in this order)
1. **Rotate the SignalWire API token** in the SignalWire dashboard first - the current one
   has been pasted into chat and a screenshot. Use the NEW token below.
2. **Confirm 10DLC** in the SignalWire dashboard: Messaging → Campaign Registry → the
   campaign is *approved* and BOTH numbers are assigned to it. Without this, outbound is
   blocked by SignalWire; inbound still works.
3. `/opt/csaas/.env` (I never write it):
   ```
   SIGNALWIRE_PROJECT_ID=9c2ec6d5-b851-4091-8b4b-c7ce0a845f87
   SIGNALWIRE_API_TOKEN=<new token>
   SIGNALWIRE_SPACE_URL=sabine.signalwire.com
   SIGNALWIRE_WEBHOOK_URL=https://csaas.sabinepropertygroup.net/api/v1/webhooks/signalwire/messaging
   ```
   Bare host for the space, no scheme, no trailing slash (the SSRF guard rejects anything
   else). `SIGNALWIRE_ENABLED` can stay unset - presence of credentials enables it.
4. `bash deploy/deploy.sh` - ships migrations 0041-0043, the voice adapter, and this phase.
5. `bash deploy/signalwire_number_webhooks.sh` on the box (or I run it over SSH once you
   confirm - it changes settings in your SignalWire account).
6. Numbers page → Add number → `+16824231003`, then `+14692103654`. Each should appear with
   carrier **signalwire** (the auto-detect) and get its inbox.

## Test spec
Unit (`pytest backend/tests/test_signalwire_messaging.py backend/tests/<numbers tests>`):
- [ ] `test_send_requests_a_delivery_receipt` → form carries `StatusCallback` = the
      configured webhook URL, and no `StatusCallback` at all when the URL is unset.
- [ ] `test_delivery_receipt_is_parsed` → a `MessageStatus=delivered` callback becomes
      `message-delivered`; `undelivered` becomes `message-failed` (parser already does this;
      pin it).
- [ ] `test_add_number_detects_the_owning_carrier` → registry with a Bandwidth fake that
      says False and a SignalWire fake that says True; POST `{e164}` → row carrier is
      `signalwire`, inbox created.
- [ ] `test_add_number_refuses_a_number_nobody_owns` → all fakes say False → 422, no row.
- [ ] `test_add_number_refuses_an_ambiguous_number` → two fakes say True → 422.
- [ ] `test_add_number_with_a_named_carrier_is_unchanged` → existing tests still green.
- [ ] Existing suites: `test_p17_provider_accounts.py`, `test_p18_number_provisioning.py`,
      `test_signalwire_voice.py` green.

Integration (on the box, after steps 1-6):
- [ ] Text the 682 number from a phone → thread appears in the inbox for that number
      within seconds; `docker logs csaas-api-1` shows a verified SignalWire webhook (no
      `signature_mismatch`).
- [ ] Send a text FROM the 682 number to the operator's phone → received; within ~10 s the
      message shows **delivered** (the receipt), not just sent.
- [ ] Repeat both for the 469 number.
- [ ] Call the 682 number → hear the "not yet configured for inbound calls" announcement, a
      `Call` row exists with carrier `signalwire`, webhook verified. (Expected until P40.)
- [ ] If outbound is rejected with a campaign/10DLC error → step 2 is not done; nothing in
      the code is wrong.

Pass criteria: unit items green before commit; integration items gate "live".

## Deploy
yes (steps above). Rollback: unset the four `SIGNALWIRE_*` values, redeploy; the numbers
can be deleted from the Numbers page. Code changes are additive and inert without creds.

## Next slice (not this phase)
**P40 — SignalWire trunk spike:** prove, in one day on the box, that a SignalWire Domain
App can (a) deliver an inbound call to `sip:144.126.152.175:5060` into a LiveKit room and
(b) accept an outbound INVITE from livekit-sip and complete it to the PSTN with a
SignalWire number as caller ID. (b) is the undocumented half. If both work: per-carrier
trunk id in `voice_plane`, second `lk sip outbound create`, and the 682/469 numbers become
softphone- and AI-capable with failover. If (b) fails: SignalWire stays a texting carrier.
