# Phase 18 — Number search, purchase, release & inventory (all 5 providers)

## Goal
From the Numbers page an admin searches available numbers at ANY provider the org has
live (env or P17 DB account), buys one, sees what it costs, and it lands as an
`org_numbers` row + auto-created inbox (P15) linked to the provider account that bought
it. Async orders (Bandwidth) are polled to completion. Release works for every provider.

## What already exists (do not rebuild)
- `GET /numbers/available`, `POST /numbers/order`, `DELETE /numbers/{id}`,
  `PATCH /numbers/{id}/campaign` in `app/api/routes/numbers.py`.
- `NumberProvider` Protocol (`app/providers/numbers.py`: search_numbers / order_number /
  release_number) implemented by Telnyx, Twilio, Plivo mixins.
- Per-org carriers via the P17 proxy: `request.app.state.carriers` already resolves
  DB-backed adapters, so purchasing with DB credentials works once a provider
  implements the Protocol.
- Frontend `NumbersPage.tsx` with search + order + release + campaign assign.

## Gaps this phase closes
1. **Bandwidth** `NumberProvider` mixin (`app/providers/bandwidth/numbers.py`): search
   `GET /accounts/{id}/availableNumbers` (areaCode/quantity/tollFree), order
   `POST /accounts/{id}/orders` (XML body; response = order id, status "RECEIVED"),
   `order_status(provider_ref)` `GET /accounts/{id}/orders/{orderId}` → COMPLETE/FAILED,
   release `POST /accounts/{id}/disconnects` (XML). Bandwidth Numbers API is XML —
   use `xml.etree` with explicit escaping; Basic auth (api_username/api_password);
   base `https://dashboard.bandwidth.com/api`.
2. **SignalWire** mixin (`app/providers/signalwire/numbers.py`): LaML-compatible —
   `GET https://{space_url}/api/laml/2010-04-01/Accounts/{project_id}/AvailablePhoneNumbers/US/{Local|TollFree}.json`,
   `POST .../IncomingPhoneNumbers.json` (PhoneNumber=), `DELETE .../IncomingPhoneNumbers/{sid}.json`;
   Basic auth project_id/api_token. Mirror the Twilio mixin.
3. **Protocol extension** (`app/providers/numbers.py`): `AvailableNumber`/`OrderResult`
   gain `monthly_cost_cents: int | None`, `setup_cost_cents: int | None`; optional
   `order_status(provider_ref) -> OrderResult` (hasattr-checked; Bandwidth implements it).
4. **Persist purchase facts** (Fable: migration 0019 + model): `org_numbers` +=
   `provider_account_id` (FK provider_accounts, SET NULL), `purchase_cost_cents`,
   `monthly_cost_cents`, `purchased_at`, `order_detail` (String 512, last provider
   status/error). Order route fills them; `provider_account_id` = the org's active
   account for that carrier if the adapter came from the DB (helper in
   `services/provider_accounts.py: active_account_for(session, provider)`), else NULL.
5. **Async order polling**: sweeper hook `poll_pending_number_orders(session)` in
   `services/number_orders.py` — every pass, for `org_numbers.status == "pending"` with a
   provider whose adapter has `order_status`, poll; COMPLETE → status active (+ inbox
   already exists), FAILED → status failed + order_detail; bounded per pass (25) and
   per org (commit per row per the StaticPool rule). Wire into `services/sweeper.py`
   exactly like the other sweeper counters (`number_orders_polled`).
6. **Release** for Bandwidth/SignalWire via the mixins; `DELETE /numbers/{id}` unchanged.
7. **Frontend NumbersPage** (dark shell like P16/P17): carrier dropdown built from live
   carriers (`/routing/catalog` env-live ∪ active P17 accounts); all five providers
   selectable when live; search results show monthly/setup cost; list shows carrier,
   provider-account label, status incl. `pending`/`failed` badges (auto-refetch every
   15s while any pending), monthly cost, purchased date; after a successful order show a
   "Grant this inbox →" link to `/settings/inboxes`; release confirm unchanged.
   `useAvailableNumbers`/`useOrderNumber` hooks extended for the new fields.

## Guardrails (implementer)
ALLOWED: app/providers/bandwidth/numbers.py (new), app/providers/signalwire/numbers.py
(new), app/providers/bandwidth/adapter.py + app/providers/signalwire/adapter.py (ONLY to
mix the new class in), app/providers/numbers.py (cost fields + optional order_status),
app/providers/telnyx|twilio|plivo/numbers.py (ONLY to populate the new cost fields),
app/api/routes/numbers.py, app/services/number_orders.py (new),
app/services/sweeper.py (hook + counter only), app/services/provider_accounts.py
(`active_account_for` helper only), tests/test_p18_*.py, frontend/src/pages/NumbersPage.tsx
+ test, frontend/src/api/hooks.ts (number hooks only).
FORBIDDEN: models/, migrations/ (Fable), everything else. No new deps (xml.etree is stdlib).

## Test Spec
- [ ] Bandwidth search/order/order_status/release against `httpx.MockTransport` with
      realistic XML fixtures; order → pending; poll COMPLETE → active; FAILED → failed +
      detail
- [ ] SignalWire search/order/release (JSON fixtures); order → active
- [ ] Telnyx/Twilio/Plivo cost fields populated (cents parsing: "1.00" → 100; missing → None)
- [ ] `POST /numbers/order` persists provider_account_id (DB-backed carrier) vs NULL
      (env carrier), purchase/monthly cents, purchased_at, creates Inbox
- [ ] sweeper `poll_pending_number_orders`: bounded, commit per row, org-scoped, idempotent
- [ ] release via each provider; released numbers keep history (status released)
- [ ] RBAC numbers:manage on order/release; numbers:read on list/search
- [ ] frontend: carrier dropdown from live set incl. DB accounts; costs rendered; pending
      badge + refetch; grant link after order; release confirm
Pass criteria: all new + full backend (985) + frontend (101) suites green.

## Deploy
yes (migration 0019).
