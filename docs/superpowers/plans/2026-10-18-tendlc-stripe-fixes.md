# 10DLC Stripe fixes - Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** (A) A workspace whose campaign was declined resubmits a campaign under its already
approved business and pays only the campaign review + the $5 service fee + 3 months, never the
brand fee again. (B) Once the 3 prepaid months run out, the 10DLC monthly fee rides on the
workspace's monthly PLAN subscription (same renewal invoice) instead of its own subscription.

**Owner rules (2026-10-17):** brand $6 (4.50 cost), campaign review flat $18, monthly $12.50
standard / $3.50 sole proprietor, $5 service fee on EVERY submission incl. resubmissions.
"These will be billed on the renewal of the package subscription, in that invoice."

## Global Constraints
- Worktree `C:/Users/omer_/csaas_swg`, current branch; never `git stash` / `git checkout`.
- `cd backend && python -m pytest -q <files> -p no:cacheprovider`; `ruff check` (100 cols).
- Never refund more than was paid. Never charge a fee the customer was not shown.
- `plan_billing.parse_items` must keep refusing truly unknown items; it only skips items whose
  `metadata.kind == "tendlc_monthly"`.
- No migration (new facts live in `TenDlcRegistration.filing` JSON).

## Review Focus
1. A campaign-only resubmit's due-today = review + service (after discount) + 3 x monthly;
   the brand is not refiled and no OTP is re-sent.
2. Cancel / refund on a reused-brand registration never refunds the brand fee (it was not paid).
3. A plan subscription carrying the 10DLC item still parses; plan changes (`_apply`) keep it.
4. Yearly plan, or no plan, or a non-active plan: the 10DLC stays on its own subscription.
5. Campaign declined after the move: the plan item is removed (no more monthly), the plan is
   untouched.

---

### Task A: Campaign-only resubmit

**Files:** `backend/app/services/tendlc.py`, `backend/app/api/routes/registration.py`
(quotes), `frontend/src/api/registration.ts` + `TextingRegistrationForm.tsx` (show the right
total), tests `backend/tests/test_tendlc_resubmit.py`, the form's existing test file.

1. `quote(tier, service_discount_bps=0, *, brand_reused=False)`: when reused,
   `brand_fee_cents = 0` and due_today excludes it. `customer_quote` gets the same kwarg.
2. `start_checkout`: a brand that has a Telnyx ref AND `status == "approved"` is allowed
   (brand_reused=True); any other already-registered brand keeps the existing ConflictError.
   With a reused brand, a campaign whose status is `"rejected"` is allowed too (other
   registered campaigns keep the ConflictError). Skip `build_brand_payload` dry-run when
   reused. Store `filing["brand_reused"] = True`. Quote with brand_reused. Product name stays.
   Idempotency key must differ from a first submission of the same reg id (it does: new reg).
3. `_step` at `paid` with `filing.get("brand_reused")`: if the campaign still holds a Telnyx
   ref (the declined one), move it: `refs["telnyx_previous"] = [*old, ref]`, drop `"telnyx"`
   and the campaign filing marker (`_CAMPAIGN_MARKER`), set status back to `"draft"` directly
   (not via advance_status if transitions refuse it; read registration.advance_status first).
   Then `reg.stage = "brand_approved"`; the existing branch files the campaign.
4. Refund math (`cancel`, `_refund_unused`, the `kept` text) uses
   `quote(reg.fee_tier, brand_reused=bool(filing.get("brand_reused")))`. In `cancel`, a reused
   registration still at `paid` (campaign ref not yet archived) refunds review + 3 months.
5. `handle_carrier_event`: order candidate rows newest first (`created_at desc`) so brand events
   for a reused brand land on the newest registration.
6. Route `GET /registration/texting`: add `quotes_resubmit` = the same per-tier dict computed
   with brand_reused=True. Frontend: when the chosen brand's status is `approved`, show
   `quotes_resubmit[tier].due_today_cents` and the line "Your business is already approved -
   you pay only for the campaign review." Keep everything else.

Tests (write first): reused brand checkout amount (standard: 1800+500+3*1250 = 6050; sole:
1800+500+3*350 = 3350); paid -> brand_approved without a brand POST and without OTP;
old campaign ref moved to telnyx_previous and a NEW campaignBuilder POST happens; a not-approved
registered brand still 409s; `_refund_unused`/cancel amounts for reused; existing self-serve
tests unchanged.

### Task B: Monthly fee on the plan subscription

**Files:** `backend/app/services/tendlc.py`, `backend/app/services/plan_billing.py`
(`parse_items` skip), tests `backend/tests/test_tendlc_plan_monthly.py` (+ run
`tests/test_plan_billing*.py`).

1. `parse_items`: an item whose `metadata.get("kind") == "tendlc_monthly"` is skipped (not
   counted, not an interval clash). Everything else unchanged.
2. New `async def move_monthly_to_plan(session, settings, reg) -> bool` in tendlc.py, run from
   `_step` at stage `active` (cheap guard first: only when
   `not filing.get("monthly_item_id")` and `now >= paid_at + 30*UPFRONT_MONTHS days - 2 days`).
   - Find the org's plan subscription (`plan_billing.entitlement` / subscriptions mirror). Only
     if it exists, is `active`/`trialing`, and its interval is month. Otherwise return False
     (the 10DLC subscription keeps billing alone - nothing changes).
   - `stripe.SubscriptionItem.create(subscription=<plan sub id>, price_data={currency usd,
     unit_amount quote(...)["monthly_cents"], recurring {interval month}, product=<product id>},
     quantity=1, metadata={"kind": "tendlc_monthly", "registration_id": str(reg.id)},
     proration_behavior="create_prorations", idempotency_key=f"tendlc-monthly-{reg.id}")`.
     `price_data` on a subscription item needs a Product id: create/reuse one product
     "Texting registration, monthly" via `stripe.Product.create(..., idempotency_key=
     "tendlc-monthly-product")` and remember its id in a module-level cache per process.
   - Then cancel the 10DLC's own subscription (`reg.subscription_id`, still in trial, so it
     never charged) with `prorate=False`. Keep `reg.subscription_id` (refund code reads its
     first invoice). Save `filing["monthly_item_id"]`, `filing["monthly_on"] = <plan sub id>`.
   - Any Stripe error: log `tendlc_monthly_move_failed`, return False, retry next pass. The
     item create comes first; if the cancel fails, the next pass sees `monthly_item_id` set and
     only retries the cancel (guard on the 10DLC subscription status).
3. `_cancel_monthly` (campaign declined) and `cancel`: if `filing.get("monthly_item_id")`,
   `stripe.SubscriptionItem.delete(item_id, proration_behavior="none")` instead of/in addition
   to cancelling `reg.subscription_id` (which is then already canceled - skip it).
4. Plan subscription deleted (plan_billing / subscriptions handler for
   `customer.subscription.deleted`): for an `active` registration whose
   `filing["monthly_on"]` equals that subscription, create a standalone 10DLC subscription
   for the same customer (`stripe.Subscription.create(customer, items=[price_data monthly],
   metadata={"kind":"tendlc_fee","registration_id"}, idempotency_key=...)`), set
   `reg.subscription_id`, clear `monthly_item_id`/`monthly_on`. On failure: `card_risk.open_alert(
   ..., "tendlc_monthly_orphan", {...})` so ops sees an unpaid campaign.

Tests (stub Stripe like `tests/test_tendlc_self_serve.py`'s `stripe_stub`): parse_items skips
the tendlc item and still refuses another unknown item; move happens only for an active
monthly plan and only after the 3 months; yearly plan -> no move; the item create precedes the
own-subscription cancel; campaign declined after the move deletes the item and leaves the plan;
plan deleted -> standalone subscription recreated; failure -> ops alert.
