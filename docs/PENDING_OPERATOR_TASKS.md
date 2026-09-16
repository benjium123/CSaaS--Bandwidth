# Pending operator tasks

Things only the operator can do, in the order that matters. Kept here so they survive a
session. Tick them off by deleting the line. Updated 2026-09-16.

## 1. Delivery receipts on Bandwidth and Telnyx (P41)
The receipts check right after the P41 deploy showed NO delivery receipt has ever reached this
server on ANY carrier. Bandwidth and Telnyx have carried real traffic, so their dashboard-side
webhook URLs were never pointed here; every text on them has shown "sent" forever.
- [ ] Bandwidth: Messaging → Applications → the messaging application → callback URL =
      `https://csaas.sabinepropertygroup.net/api/v1/webhooks/bandwidth/messaging`
- [ ] Telnyx: Messaging → Programmable Messaging → the messaging profile → webhook URL =
      `https://csaas.sabinepropertygroup.net/api/v1/webhooks/telnyx/messaging`
- [ ] Twilio (if ever used): the number's status callback → `.../webhooks/twilio/messaging`
- [ ] Send one text on each carrier, then ask Fable to run the receipts check
      (`GET /api/v1/platform/messaging/receipts-check`, operator token) - a recent
      `last_receipt_at` per carrier is the proof.
Plivo and SignalWire need nothing: the code requests the receipt per message.

## 2. SignalWire go-live (P39)
- [ ] The test text to +1 469 461 7576 was refused: `422 To must send to a verified caller id`
      = trial-space restriction. Either verify that number under Phone Numbers → Verified
      Numbers, or upgrade the space. Then tell Fable to resend.
- [ ] Rotate the API token in the SignalWire dashboard (the old one was pasted into chat and
      a screenshot). Put the new value in `/opt/csaas/.env` as `SIGNALWIRE_API_TOKEN`, then
      tell Fable to recreate the api + agent containers (or run the command in RUNBOOK
      "SignalWire go-live").
- [ ] Confirm 10DLC: Messaging → Campaign Registry → campaign APPROVED and BOTH numbers
      (+16824231003, +14692103654) assigned. Without it SignalWire blocks outbound texts.
- [ ] Numbers page → Add number → `+16824231003`, then `+14692103654`. Each should show
      provider SignalWire and get its own inbox.

## 3. Telnyx concurrent-channel limit (P38)
- [ ] In the Telnyx portal that owns the LiveKit trunk connection (NOT the one whose key is
      in /opt/acq-voice/.env - that account shows "REI CRM SIP", "Call Test Program",
      "Forward Only" only): Voice → SIP Trunking → the FQDN connection pointed at
      144.126.152.175:5060 → read inbound/outbound channel limits. Pay-as-you-go = no cap,
      nothing to do. A small number = raise it.

## 4. Platform operator token (new 2026-09-16)
- [ ] Read it on the server: `grep PLATFORM_OPS_TOKEN /opt/csaas/.env`. Paste it into the
      ops sections of the Platform page (billing ops, messaging health). It had never been
      set before; every ops feature returned "not configured".

## Later, not urgent
- Package prices / allowances / overage rates (P37c engine is live but no plans seeded).
- SignalWire trunk spike (P40) so calls on the 682/469 numbers reach the softphone + AI.
- Second AI-worker VPS when concurrent AI calls pass ~10.
- Messaging-health thresholds: tune after seeing real numbers (defaults in RUNBOOK).
