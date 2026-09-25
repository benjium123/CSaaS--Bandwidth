/**
 * The admin "Console" tab: cross-tenant billing v2 visibility (every org's purchases,
 * discounts, usage, traffic, blocked attempts, profit) plus the price list and the manual
 * credit tools. Backend: app/api/routes/ops_console.py + app/services/console.py.
 *
 * Money is integer micros everywhere (1_000_000 = $1) - reuse spend.ts's micros<->dollar
 * helpers rather than re-deriving rounding here.
 *
 * Metric dicts (`ConsoleMetrics`) are intentionally `Record<string, number>`: the backend
 * only emits a key when it is non-zero for the window (see console.py's `finish()` /
 * `org_metrics()`), and it emits a `carrier_<name>_texts` / `carrier_<name>_calls` pair per
 * carrier actually seen - a fixed interface would have to guess carrier names.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient } from "./client";
import { fetchAuthedBlob } from "./client";

export type ConsoleMetrics = Record<string, number>;

export interface ConsoleOrgRow {
  org_id: string;
  name: string;
  slug: string;
  created_at: string | null;
  prepaid: boolean;
  billing_state: string;
  balance_micros: number;
  warn_threshold_micros: number;
  avg_daily_spend_micros: number;
  auto_recharge: boolean;
  auto_recharge_failures: number;
  sms_bundle_units: number;
  mms_bundle_units: number;
  voice_bundle_minutes?: number;
  numbers: number;
  plan_code: string | null;
  metrics: ConsoleMetrics;
}

export interface ConsoleSummary {
  orgs: number;
  prepaid_orgs: number;
  low_orgs: number;
  exhausted_orgs: number;
  balances_micros: number;
  active_numbers: number;
  sms_bundle_units_outstanding: number;
  mms_bundle_units_outstanding: number;
  voice_bundle_minutes_outstanding?: number;
}

export interface ConsoleOrgsResponse {
  start: string;
  end: string;
  orgs: ConsoleOrgRow[];
  totals: ConsoleMetrics;
  summary: ConsoleSummary;
}

export interface ConsoleOrgDetailOrg {
  org_id: string;
  name: string;
  slug: string;
  prepaid: boolean;
  billing_state: string;
  balance_micros: number;
  warn_threshold_micros: number;
  avg_daily_spend_micros: number;
  /** The raw `credit_auto_recharge` JSON blob (org detail), NOT the boolean the orgs table
   * row exposes under the same name - `auto.enabled` is the on/off flag inside it. */
  auto_recharge: { enabled?: boolean; [key: string]: unknown } | null;
  auto_recharge_failures: number;
  sms_bundle_units: number;
  mms_bundle_units: number;
  voice_bundle_minutes?: number;
  telnyx_billing_group_id: string | null;
}

export interface ConsoleSeriesPoint {
  date: string;
  sms_out_segments: number;
  sms_in_segments: number;
  mms_out: number;
  mms_in: number;
  calls_out: number;
  calls_in: number;
  minutes_out: number;
  minutes_in: number;
  usage_revenue: number;
  paid: number;
  blocked_total: number;
}

export interface ConsoleLedgerEntry {
  seq: number;
  type: string;
  amount_micros: number;
  balance_after_micros: number;
  reference: string | null;
  note: string | null;
  at: string | null;
}

export interface ConsolePayment {
  id: string;
  org_id: string;
  kind: string;
  state: string;
  quantity: number;
  list_micros: number;
  paid_micros: number;
  discount_micros: number;
  stripe_fee_micros: number | null;
  credited_micros: number;
  units_credited: number;
  stripe_payment_intent_id: string | null;
  paid_at: string | null;
  created_at: string | null;
  /** Only present on the flat /payments list (it joins Org.name); absent on an org
   * detail's own `payments` array, where the org is already known from context. */
  org_name?: string;
}

export interface ConsoleRefusal {
  kind: string;
  reason: string | null;
  price_micros: number;
  balance_micros: number;
  detail: string | null;
  at: string | null;
}

export interface ConsoleNumberRow {
  e164: string;
  carrier: string | null;
  status: string;
  rental_paid_through: string | null;
  billing: unknown;
}

export interface ConsoleOrgDetail {
  org: ConsoleOrgDetailOrg;
  start: string;
  end: string;
  metrics: ConsoleMetrics;
  series: ConsoleSeriesPoint[];
  ledger: ConsoleLedgerEntry[];
  payments: ConsolePayment[];
  refusals: ConsoleRefusal[];
  numbers: ConsoleNumberRow[];
}

export interface ConsolePrice {
  metric: string;
  price_micros: number;
  default_micros: number | null;
  note: string | null;
  updated_at: string | null;
}

export interface ConsoleAdjustResult {
  balance_after_micros: number;
}

export interface ConsoleBundleGrantResult {
  units_after: number;
}

export interface ConsolePrepaidResult {
  prepaid: boolean;
}

export interface ConsolePriceUpdateResult {
  metric: string;
  price_micros: number;
  previous_micros: number;
}

export type DateRange = { start: string; end: string };

function rangeParams({ start, end }: DateRange): string {
  const search = new URLSearchParams({ start, end });
  return `?${search.toString()}`;
}

// ---------------------------------------------------------------------------------------
// Fetchers
// ---------------------------------------------------------------------------------------

export async function fetchConsoleOrgs(
  api: ApiClient,
  range: DateRange,
): Promise<ConsoleOrgsResponse> {
  return api.request<ConsoleOrgsResponse>(`/api/v1/ops/console/orgs${rangeParams(range)}`);
}

export async function fetchConsoleOrg(
  api: ApiClient,
  orgId: string,
  range: DateRange,
): Promise<ConsoleOrgDetail> {
  return api.request<ConsoleOrgDetail>(
    `/api/v1/ops/console/orgs/${orgId}${rangeParams(range)}`,
  );
}

export async function fetchConsolePayments(
  api: ApiClient,
  range: DateRange,
): Promise<ConsolePayment[]> {
  const res = await api.request<{ payments: ConsolePayment[] }>(
    `/api/v1/ops/console/payments${rangeParams(range)}`,
  );
  return res.payments;
}

export async function fetchConsolePrices(api: ApiClient): Promise<ConsolePrice[]> {
  const res = await api.request<{ prices: ConsolePrice[] }>("/api/v1/ops/console/prices");
  return res.prices;
}

export async function updateConsolePrice(
  api: ApiClient,
  metric: string,
  priceMicros: number,
  note?: string,
): Promise<ConsolePriceUpdateResult> {
  return api.request<ConsolePriceUpdateResult>(`/api/v1/ops/console/prices/${metric}`, {
    method: "PUT",
    json: { price_micros: priceMicros, note: note ?? null },
  });
}

export async function adjustConsoleOrg(
  api: ApiClient,
  orgId: string,
  amountMicros: number,
  note: string,
): Promise<ConsoleAdjustResult> {
  return api.request<ConsoleAdjustResult>(`/api/v1/ops/console/orgs/${orgId}/adjust`, {
    method: "POST",
    json: { amount_micros: amountMicros, note },
  });
}

export async function grantConsoleBundle(
  api: ApiClient,
  orgId: string,
  kind: "sms" | "mms" | "voice",
  units: number,
  note: string,
): Promise<ConsoleBundleGrantResult> {
  return api.request<ConsoleBundleGrantResult>(`/api/v1/ops/console/orgs/${orgId}/bundles`, {
    method: "POST",
    json: { kind, units, note },
  });
}

export async function setConsolePrepaid(
  api: ApiClient,
  orgId: string,
  enabled: boolean,
): Promise<ConsolePrepaidResult> {
  return api.request<ConsolePrepaidResult>(`/api/v1/ops/console/orgs/${orgId}/prepaid`, {
    method: "POST",
    json: { enabled },
  });
}

/** The CSV needs the auth headers, so it cannot simply be an <a href> the browser follows
 * (see api/contactsPro.ts's downloadContactExport for the same shape). */
export async function downloadConsoleOrgsCsv(api: ApiClient, range: DateRange): Promise<void> {
  const blob = await fetchAuthedBlob(
    api,
    `/api/v1/ops/console/orgs.csv${rangeParams(range)}`,
  );
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `orgs_${range.start}_${range.end}.csv`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

// ---------------------------------------------------------------------------------------
// react-query hooks
// ---------------------------------------------------------------------------------------

const CONSOLE_KEY = ["ops", "console"] as const;

export function useConsoleOrgs(api: ApiClient, range: DateRange) {
  return useQuery({
    queryKey: [...CONSOLE_KEY, "orgs", range.start, range.end],
    queryFn: () => fetchConsoleOrgs(api, range),
    enabled: Boolean(range.start && range.end),
  });
}

export function useConsoleOrg(
  api: ApiClient,
  orgId: string | null,
  range: DateRange,
) {
  return useQuery({
    queryKey: [...CONSOLE_KEY, "org", orgId, range.start, range.end],
    queryFn: () => fetchConsoleOrg(api, orgId as string, range),
    enabled: Boolean(orgId && range.start && range.end),
  });
}

export function useConsolePayments(api: ApiClient, range: DateRange) {
  return useQuery({
    queryKey: [...CONSOLE_KEY, "payments", range.start, range.end],
    queryFn: () => fetchConsolePayments(api, range),
    enabled: Boolean(range.start && range.end),
  });
}

export function useConsolePrices(api: ApiClient) {
  return useQuery({
    queryKey: [...CONSOLE_KEY, "prices"],
    queryFn: () => fetchConsolePrices(api),
  });
}

export function useUpdateConsolePrice(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ metric, priceMicros, note }: { metric: string; priceMicros: number; note?: string }) =>
      updateConsolePrice(api, metric, priceMicros, note),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...CONSOLE_KEY, "prices"] });
    },
  });
}

/** Shared invalidation for every write that changes an org's money/state: the orgs table,
 * that org's own detail, and (for a price change) every org's numbers move together, but a
 * credit/bundle/prepaid change is scoped to one org - invalidating the whole console key is
 * simplest and correct either way, since these are cheap reads behind a date range. */
function invalidateConsole(qc: ReturnType<typeof useQueryClient>) {
  void qc.invalidateQueries({ queryKey: CONSOLE_KEY, exact: false });
}

export function useAdjustConsoleOrg(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ orgId, amountMicros, note }: { orgId: string; amountMicros: number; note: string }) =>
      adjustConsoleOrg(api, orgId, amountMicros, note),
    onSuccess: () => invalidateConsole(qc),
  });
}

export function useGrantConsoleBundle(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      orgId,
      kind,
      units,
      note,
    }: {
      orgId: string;
      kind: "sms" | "mms" | "voice";
      units: number;
      note: string;
    }) => grantConsoleBundle(api, orgId, kind, units, note),
    onSuccess: () => invalidateConsole(qc),
  });
}

export function useSetConsolePrepaid(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ orgId, enabled }: { orgId: string; enabled: boolean }) =>
      setConsolePrepaid(api, orgId, enabled),
    onSuccess: () => invalidateConsole(qc),
  });
}

// ---------------------------------------------------------------------------------------
// Small display helpers
// ---------------------------------------------------------------------------------------

/** Per-carrier text/call counts, read off the dynamic `carrier_<name>_texts` /
 * `carrier_<name>_calls` metric keys (console.py's `org_metrics`). A carrier with only one
 * of the two still gets a row, with the other side at 0. */
export function carrierCounts(
  metrics: ConsoleMetrics,
): { carrier: string; texts: number; calls: number }[] {
  const byCarrier = new Map<string, { texts: number; calls: number }>();
  for (const [key, value] of Object.entries(metrics)) {
    const textsMatch = /^carrier_(.+)_texts$/.exec(key);
    const callsMatch = /^carrier_(.+)_calls$/.exec(key);
    if (textsMatch) {
      const name = textsMatch[1];
      const row = byCarrier.get(name) ?? { texts: 0, calls: 0 };
      row.texts = value;
      byCarrier.set(name, row);
    } else if (callsMatch) {
      const name = callsMatch[1];
      const row = byCarrier.get(name) ?? { texts: 0, calls: 0 };
      row.calls = value;
      byCarrier.set(name, row);
    }
  }
  return Array.from(byCarrier.entries())
    .map(([carrier, counts]) => ({ carrier, ...counts }))
    .sort((a, b) => a.carrier.localeCompare(b.carrier));
}

const PRICE_UNIT_LABELS: Record<string, string> = {
  sms_out: "per SMS segment sent",
  mms_out: "per MMS sent",
  number_mrc: "per number / month",
  sms_bundle: "per 1,000 SMS bundle",
  mms_bundle: "per 100 MMS bundle",
  number_setup: "one-time per number",
};

/** The price list's per-unit label. `voice_min_*` and `fax_page_*` are families (in/out,
 * per-provider variants), so they are matched by prefix rather than listed one by one. */
export function priceUnitLabel(metric: string): string {
  if (metric in PRICE_UNIT_LABELS) return PRICE_UNIT_LABELS[metric];
  if (metric.startsWith("voice_min_")) return "per minute";
  if (metric.startsWith("fax_page_")) return "per page";
  return "";
}

/** Dollars with up to 4 decimal places (a per-segment price is often sub-cent) <-> micros.
 * Distinct from spend.ts's dollarsToMicros/formatMicros, which round to the cent and are
 * for money customers pay, not for editing a unit price. */
export function priceMicrosToDollarsString(micros: number): string {
  return (micros / 1_000_000).toFixed(4).replace(/0+$/, "").replace(/\.$/, "") || "0";
}

export function priceDollarsStringToMicros(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  if (!Number.isFinite(parsed) || parsed < 0) return null;
  return Math.round(parsed * 1_000_000);
}
