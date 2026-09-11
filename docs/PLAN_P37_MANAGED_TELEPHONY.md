# P37 — Managed telephony (reseller): numbers, SMS and calling for every new company

Fable 2026-09-11. Status: **PLAN — awaiting operator approval.** No code until approved.

## Decision (operator, 2026-09-11)
Reseller model, like OpenPhone / MightyCall. When a company signs up, OUR Telnyx master account
creates a Telnyx **Managed Account** (sub-account) for it. We buy its numbers, provision SMS and
voice inside that sub-account, and **bill the company ourselves**. Customers never see or touch Telnyx.

## External prerequisites (operator-owned — these block go-live, not the build)
1. **Managed Accounts approval.** Telnyx must approve the master account as a manager and it must
   pass Level-2 verification. This is not self-service; request it from Telnyx sales/support.
2. **Rollup billing on.** All sub-account spend then bills to the master balance and we re-bill
   customers. It is off by default.
3. **10DLC as an ISV.** Sub-accounts cannot register their own brands. The master registers one
   brand plus one campaign per customer. Telnyx passes fees through at cost: about $4.50 per brand,
   $15 per campaign vetting, and $1.50–$10 per campaign per month, plus carrier per-message fees.
   As the ISV we carry the TCR compliance duty: vet the customer, keep opt-in records for 4+ years,
   and enforce STOP/HELP.
4. **Quota.** The default is 1,000 managed accounts; ask Telnyx for more before we approach it.
5. **Platform secrets** in `.env`: the master API key and the webhook public key. Operator sets
   these; they are never stored per org.

**Unverified. Confirm with Telnyx before P37a starts.** Each answer changes the design:
- Can the master key act on a sub-account via a header, or must we store each sub-account's own
  API key (encrypted)? The plan assumes the latter, which is safe either way.
- Can usage reports be filtered per managed account from the master, or must we pull with each
  sub-account's key?
- What does Telnyx do to a sub-account with a negative balance under rollup billing?
- Is inbound to LiveKit best done with an **FQDN connection** or a **credential connection**? The
  current hand-built setup uses a credential connection, and its **inbound path has never been tested**.

## What exists today, and why it does not scale past one company
- Your test org's Telnyx setup was hand-built:
  - messaging profile *Test SMS Profile*;
  - SIP credential connection *Call Test Program*;
  - one LiveKit outbound trunk `telnyx-out` logged into that credential;
  - one inbound trunk and dispatch rule listing only `+14693818973`.
- The backend dials through ONE global `livekit_sip_outbound_trunk_id`. A second company's browser
  calls would therefore go out through your Telnyx login, with your caller-ID pool.
- Providers are per org and bring-your-own (`provider_accounts`). There is no managed-account concept.
- Money foundations already exist:
  - P24 credit ledger (`services/credits.py`, append-only), Stripe top-ups and payment methods, provider rate sheet;
  - P32 schema committed in migration 0034: `plans` (`included` allowances, `overage_rates`),
    `invoices`, `invoice_lines`, and `orgs.plan_code`. No P32 services have been built yet.

## Architecture

### 1. Provisioning engine — `services/telephony_provisioning.py` (Fable reviews every line)
An idempotent, resumable state machine per org. Every step persists the id it created. Re-running
skips completed steps; this is also the customer's **"Repair setup"** button. A failure stops at that
step with a plain-English reason, e.g. "Could not create your texting profile: …".
1. **Managed account.** Create it with `business_name` = org name. Retrieve and store its API key
   encrypted with `CREDENTIALS_MASTER_KEY`.
2. **Messaging profile** inside it:
   - `webhook_url` = `{PUBLIC_BASE_URL}/api/v1/webhooks/telnyx/messaging`, plus a failover URL;
   - whitelisted destinations US/CA.
   This closes the manual step that silently dropped your inbound SMS today.
3. **Outbound voice profile**: US/CA destinations, with a spend limit set from the customer's plan.
4. **SIP connection** for LiveKit: outbound digest credentials we generate, plus the inbound route to our SIP ingress. Attach the outbound voice profile.
5. **LiveKit.** Give the org **its own outbound trunk** (`sip.telnyx.com`, the org's SIP credential).
   Add the org's numbers to inbound routing, and create a dispatch rule. Store the trunk and rule ids on the org.
6. **Numbers.** Search and order inside the sub-account using the existing `TelnyxNumbers.order_number`
   with the sub-account's key. Assign each number to the messaging profile and the voice connection,
   and register it on the LiveKit trunks.
7. **10DLC.** The master registers the brand and campaign for the org, assigns its numbers, and polls status.
   The existing "Register for texting" checklist step tracks it.

### 2. Voice routing
- `start_room_call` resolves the **org's own** outbound trunk. The global setting remains only as a fallback
  for the legacy hand-built org.
- Inbound: one shared LiveKit inbound trunk plus a per-org dispatch rule scoped to that org's numbers,
  so no org can ever ring another's room.
- Isolation is a hard test requirement: org B never dials via org A's trunk.

### 3. Billing (builds P32 on the P24 ledger; all money code Fable-owned)
- **Cost ingestion.** A nightly pull of per-sub-account usage (SMS/MMS detail records and call detail
  records) into usage events carrying `cost_micros`. The pull is idempotent on the Telnyx record id.
- **What customers are charged:**
  - monthly plan fee;
  - per-number monthly rental;
  - overage for SMS segments, MMS, and voice minutes, charged after plan allowances net first,
    at the rate-sheet price including markup;
  - 10DLC fees passed through.
  Customers only ever see prices, never costs or margin.
- **Collection:**
  - A card on file (Stripe, P24) is required before the first number order.
  - Overage draws prepaid credits, or goes on the monthly invoice (P32).
- **Non-payment:**
  1. Dunning emails.
  2. Soft suspend: block send and dial in our app, and show a banner.
  3. After N days, hard-disable the managed account. Numbers are kept for M days before release.

### 4. Signup and onboarding (customer-facing)
1. Sign up.
2. Pick a plan.
3. Add a card.
4. **"Get a phone number"**: search by area code, then order.
5. Provisioning runs in the background with a live checklist.
6. **"Register for texting"**: a business-details form; we submit it as the ISV.
7. Ready to text and call.

For managed orgs this replaces the current "Connect a provider" step. Bring-your-own stays available only
to platform operators and the legacy org.

## Schema (Fable-only) — migration `0041_managed_telephony`
- `telephony_accounts`: one row per org.
  - `managed_account_id`
  - `status`: provisioning | active | suspended | failed
  - `api_key_encrypted`
  - `messaging_profile_id`, `outbound_voice_profile_id`
  - `sip_connection_id`, `sip_username`, `sip_password_encrypted`
  - `livekit_outbound_trunk_id`, `livekit_dispatch_rule_id`
  - `tendlc_brand_id`, `tendlc_campaign_id`
  - `last_step`, `last_error`, timestamps

  This is a separate table from `provider_accounts`: managed and bring-your-own are different trust models.
- `org_numbers.provisioning`: JSON recording the messaging-profile assignment, voice-connection
  assignment, trunk registration, and campaign assignment.
- Telephony usage: add metrics to the existing usage-event path, with `source = 'telnyx_record'` and
  the Telnyx record id as the idempotency key. No new ledger table; `credit_ledger` writes stay only in `services/credits.py`.
- Plan seed rows go in the P32 `plans` table, with prices from the operator (see "Operator decisions needed").

## Phases (vertical slices; each one deploys, everything behind `TELEPHONY_MANAGED=0` until P37d)
| Phase | Delivers | Proves |
|---|---|---|
| **P37a** | Provisioning engine steps 1–2 plus number order and assignment (SMS only) | A brand-new test org texts both ways with zero portal clicks |
| **P37b** | Steps 3–6: per-org SIP connection, LiveKit trunks, and dispatch; `start_room_call` uses the org's own trunk | The new org calls both ways; org isolation tests pass |
| **P37c** | Billing: cost ingestion, P32 plan and invoice services, number rental, card-on-file gate, dunning and suspension | A month of usage produces a correct invoice and ledger; a suspended org cannot send or dial |
| **P37d** | 10DLC as ISV, the onboarding UI (plan, card, number search, progress checklist, Repair setup), then flip the flag | A new signup goes live end-to-end in the UI |

Moving your existing test org onto a sub-account is optional and later. Moving numbers between
Telnyx accounts is a support-assisted port.

## Test spec (summary; each phase plan expands it)
- **Idempotency:** re-running provisioning creates nothing new. A failure at step N resumes at step N.
  Tested against a Telnyx MockTransport.
- **Isolation:** org B's call never uses org A's trunk; org B's inbound never rings org A. Webhooks
  resolve the org by number and tenant context.
- **Secrets:** sub-account API keys and SIP passwords are encrypted at rest and never returned by any API.
- **Money:**
  - allowances net before overage;
  - price = cost × (1 + markup);
  - `cost_micros` never appears in customer responses;
  - the ledger balance holds after invoice drawdown;
  - ingestion is idempotent on the Telnyx record id.
- **Gates:** no card means no number order (plain message); suspension blocks send and dial with a plain banner.
- **Manual (P37b):** a two-way-audio call from a freshly provisioned org, both inbound and outbound.

## Operator decisions needed before P37c (not needed to start P37a/b)
1. **Pricing:** plan tiers and monthly prices, what each plan includes (SMS segments, minutes, numbers,
   seats), per-number rental price, and overage rates.
2. **Overage collection:** prepaid credits (top-up) or post-paid on the monthly invoice.
3. **10DLC shape:** one brand plus campaign per customer (recommended; isolates compliance and throughput),
   or a shared platform campaign for small senders.
4. **Grace periods:** dunning days before soft suspend (N), and days before numbers are released (M).
