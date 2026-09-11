# P37 — Managed telephony (reseller): numbers, SMS and calling for every new company

Fable 2026-09-11. Status: **APPROVED 2026-09-11** ("please complete it"). Building.

## Decision 2 (operator, 2026-09-11): prepaid hard gate per organisation
Every org can be put on **prepaid texting and calling**: outbound SMS/MMS, outbound calls and
number orders draw from the P24 credit balance (top-ups) and are refused (402) when it cannot
cover them; inbound SMS and inbound minutes are charged but never refused; a running outbound
call is hung up when its hold can no longer be extended; number rental is charged monthly from
the balance. Built FIRST, ahead of P37a, because it applies to every org (bring-your-own too).
Switched per org by platform ops (`orgs.telephony_prepaid`, migration 0041); managed orgs will
start with it on. This also settles operator decision 2 below: **overage is prepaid credits**.
Service: `backend/app/services/telephony_billing.py`; tests: `tests/test_prepaid_telephony.py`.

## Decision 3 (operator, 2026-09-11): we sell our own packages; every price clears Telnyx + Stripe
We define our own SMS and calling packages with allotted texts, minutes and numbers, and we
charge more than Telnyx charges us because Stripe takes its fee on every top-up as well.
Shape is decided here; the actual prices and allowances are operator inputs (see the table
at the end of this section). Built in **P37c** on the P32 schema already committed in 0034.

### What a package is
One row in `plans` (P32): `monthly_price_micros`, `included = {"sms_segments", "voice_minutes",
"numbers", "seats"}`, `overage_rates = {"sms_out", "mms_out", "voice_min_out", "voice_min_in",
"number_mrc"}` (customer price per unit past the allowance). An org has one `plan_code`; the
cycle starts on `plan_started_at` and resets on its monthly anniversary. Unused allowance does
not roll over (assumption; say so if you want rollover).

### The rigorous check - how an org can never use more than it paid for
1. **One meter, one writer.** Every outbound segment, every call minute (both directions),
   every MMS and every number-month goes through `services/telephony_billing.py`, which is the
   only code that decides what a unit costs the customer, and `services/credits.py`, the only
   code that writes the ledger. The gate hooks already sit in the four places traffic can
   originate (`messaging.py`, `calls.py`, `voice_plane/service.py`, `routes/numbers.py`); a
   test asserts nothing else imports the carrier senders.
2. **Allowance counters** (migration 0043, Fable-only): `plan_allowances(org_id, period_start,
   metric, used_units)`. A unit is taken with one atomic
   `UPDATE ... SET used = used + n WHERE used + n <= included RETURNING`, so two concurrent
   sends cannot both take the last text. The counter row and the ledger entry are written in
   the same transaction as the send/dial.
3. **Decision at the moment of use, per unit.** Allowance left -> the unit is covered (price 0,
   still recorded as a usage event marked `covered_by_plan`). Allowance gone -> the unit is
   overage at the plan's `overage_rates` (fallback: rate card x markup) and the prepaid hard
   gate (Decision 2) applies: not enough credits -> 402 *before* the text is sent or the call is
   dialled; a live call reserves 5 minutes, the sweeper extends the hold every pass, and the
   call is hung up when the hold can no longer be extended. Inbound texts and minutes count
   against the allowance and are charged past it, but are never refused.
4. **Minutes are whole minutes, rounded up per call leg.** Telnyx bills in finer increments,
   so rounding is always in our favour.
5. **Numbers.** The included count nets against the monthly rental; each extra number charges
   `number_mrc` monthly from credits (`renew_number_rentals`, already built). No number order
   without the balance to cover the first month (already built).
6. **Reconciliation, nightly (P37c cost ingestion).** Pull Telnyx message and call detail
   records per sub-account (idempotent on the Telnyx record id) with `cost_micros`. Compare
   Telnyx's counted segments and minutes with our meter per org per day; drift beyond 2% raises
   an ops alert - that is how a path that bypasses the meter would be caught. Monthly per-org
   margin report: plan fee + overage + rentals - Telnyx cost - Stripe fee. Customers see
   "used 412 of 1,000 texts this cycle" and their balance; never cost or margin.

### Stripe's fee and the price floor
A top-up credits exactly what the customer paid (`amount_received x 10_000` in
`routes/webhooks.py`); Stripe's 2.9% + $0.30 comes out of our side. So the fee is carried by the
markup on every price, not by a surcharge on the top-up:
- **Unit floor:** customer price >= Telnyx cost x (1 + `MIN_MARGIN_BPS`), where the floor
  covers Stripe (~3%) plus the target margin; the current default markup is 30%
  (`DEFAULT_TRAFFIC_MARKUP_BPS`). The ops rate editor and the plan editor refuse to save a
  price below the floor ("price is below cost plus processing"), and the nightly margin report
  asserts it again against the live rate card.
- **Plan floor:** `monthly_price >= (included_sms x sms cost + included_minutes x minute cost +
  included_numbers x number cost) x (1 + MIN_MARGIN_BPS)`. Ops shows the floor at current
  Telnyx rates when editing a plan and refuses below it.
- **Minimum top-up $25** (presets 25/50/100) keeps the $0.30 fixed fee at <= 1.2%.
- Put the 10DLC carrier pass-through (about $0.003-0.005 per segment on T-Mobile/AT&T) into the
  `sms_out` cost on the rate card, so the floor includes it.

### Draft packages (operator to confirm prices; Telnyx default costs: text $0.004, minute
$0.007 out / $0.0035 in, number $1.00 per month)
| Package | Monthly | Texts | Minutes | Numbers | Telnyx cost if fully used | After Stripe (2.9% + $0.30) |
|---|---|---|---|---|---|---|
| Starter | $19 | 500 | 300 | 1 | $5.10 | ~$13.05 margin |
| Growth | $49 | 2,000 | 1,000 | 3 | $18.00 | ~$29.28 margin |
| Scale | $99 | 5,000 | 3,000 | 10 | $51.00 | ~$44.83 margin |
Overage (draft): $0.02 per text segment, $0.05 per MMS, $0.03 per minute, $2.00 per extra
number per month. Seats are not metered until P33.

**Operator inputs still needed:** package names, monthly prices, included texts / minutes /
numbers / seats, overage prices, rollover yes/no, and `MIN_MARGIN_BPS`.

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

## Schema (Fable-only) — migration `0042_managed_telephony` (0041 is the prepaid gate)
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
| **P37c** | Billing: packages (Decision 3: plan rows, `plan_allowances` 0043, allowance-first metering, price floor), cost ingestion + reconciliation, P32 invoice services, card-on-file gate, dunning and suspension | A month of usage produces a correct invoice and ledger; a suspended org cannot send or dial |
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
1. **Pricing:** shape decided (Decision 3: our own packages, allowance-first metering, price floor
   over Telnyx + Stripe). Still needed: the numbers in Decision 3's table.
2. ~~**Overage collection:**~~ **Decided: prepaid credits** (the per-org hard gate above).
3. **10DLC shape:** one brand plus campaign per customer (recommended; isolates compliance and throughput),
   or a shared platform campaign for small senders.
4. **Grace periods:** dunning days before soft suspend (N), and days before numbers are released (M).
