# Phase 19 — Provider cost tracking & spend dashboard

## Goal
Know what each backend provider costs: per-provider, per-number, per-metric daily spend,
derived from the platform's own message/call/number records × a rate card, shown on the
Providers page and as an Analytics tile. Reconciliation against provider invoices is a
later phase; P19 is the estimate the operator can act on today.

## Data model (Fable — migration 0020)
- `provider_rates` (TenantScoped): `provider`, `metric` (one of SPEND_METRICS), `unit_cost_micros` (int, cost per unit in millionths of a dollar), `currency` ("USD"), unique `(org_id, provider, metric)`. Org overrides only — code-level defaults (`DEFAULT_RATES` seeded from current public list prices, clearly marked "estimate") apply when no row exists.
- `provider_spend_daily` (TenantScoped): `period_date`, `provider`, `metric`, `quantity` (int), `cost_micros` (int), `number_id` (nullable FK org_numbers, for number-level rows), unique `(org_id, period_date, provider, metric, number_id)`. Derived daily; recomputed idempotently like `usage_records`.
- `SPEND_METRICS = ("sms_out", "sms_in", "mms_out", "mms_in", "voice_min_out", "voice_min_in", "number_mrc", "number_setup")`.

## Backend
1. `app/services/spend.py`: `rollup_day(session, org_id, day)` — counts `Message` rows by (carrier, direction, mms?) and `Call` minutes by (carrier, direction) for that day, × resolved rate (org override → default), plus `number_mrc` per active `OrgNumber` (uses `monthly_cost_cents` when set, else the provider's `number_mrc` rate; prorated by days in month) and `number_setup` on `purchased_at` day from `purchase_cost_cents`. Writes/updates `provider_spend_daily` rows (commit per org; StaticPool rule). Sweeper hook once per hour for today + yesterday (`spend_orgs_rolled_up` counter), matching the existing usage rollup cadence.
2. Routes `app/api/routes/spend.py` (`reports:read` / `settings:write` for rates):
   - `GET /api/v1/spend/summary?from=&to=` → per provider {cost_usd, by_metric{}, numbers[] {e164, cost_usd}}, total.
   - `GET /api/v1/spend/daily?from=&to=&provider=` → rows for charts.
   - `GET /api/v1/provider-rates` → effective rates (override or default, flagged), `PUT /api/v1/provider-rates` (list upsert; audit).
3. Analytics: add `spend_usd_month_to_date` to the existing analytics summary payload.

## Frontend
- Providers page: per-card "Spend this month" line + expandable by-metric breakdown; a "Rates" drawer (editable unit costs, shows default vs override).
- Numbers page: monthly cost column already exists (P18); add "Spend MTD" per number.
- Analytics/Dashboard: a spend tile (MTD total + per provider), daily bar chart for the last 30 days (use the existing charting approach in the dashboard; no new deps).

## Guardrails (implementer)
ALLOWED: app/services/spend.py (new), app/api/routes/spend.py (new), app/main.py (router),
app/services/sweeper.py (hook + counter), app/services/analytics.py (one field), tests/test_p19_*.py,
frontend/src/api/spend.ts (new), frontend/src/pages/ProvidersPage.tsx (+test), NumbersPage.tsx (+test),
DashboardPage.tsx (+test). FORBIDDEN: models/, migrations/ (Fable), everything else. No new deps.

## Test Spec
- [ ] rate resolution: override beats default; unknown provider/metric → 0 with a flag
- [ ] rollup math: N outbound SMS segments × rate; MMS separated; inbound vs outbound; voice minutes rounded up per call; number MRC prorated (28/30/31-day months); setup charged once on purchase day; idempotent re-run yields identical rows
- [ ] org isolation of spend rows; summary totals == sum(daily)
- [ ] RBAC: reports:read for reads, settings:write for rate PUT; audit on rate change
- [ ] sweeper hook runs and counts orgs
- [ ] frontend: spend line per card, rates drawer PUT body, dashboard tile renders totals, numbers MTD column
Pass criteria: all new + full suites green.

## Deploy
yes (migration 0020).
