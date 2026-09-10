import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { dollarsToMicros, formatMicros } from "./spend";
import type { ApiClient } from "./client";

export { dollarsToMicros, microsToDollars, monthToDateRange } from "./spend";

export const BILLING_SUMMARY_PATH = "/api/v1/billing/summary";
export const BILLING_LEDGER_PATH = "/api/v1/billing/ledger";
export const BILLING_USAGE_PATH = "/api/v1/billing/usage";
export const BILLING_USAGE_CALL_PATH = "/api/v1/billing/usage/calls";
export const BILLING_TOPUPS_PATH = "/api/v1/billing/topups";
export const BILLING_AUTO_RECHARGE_PATH = "/api/v1/billing/auto-recharge";
export const BILLING_RATES_PATH = "/api/v1/billing/rates";
export const BILLING_PAYMENT_METHODS_PATH = "/api/v1/billing/payment-methods";

export type BalanceWarning = "low" | "critical" | "empty";

export interface AutoRecharge {
  threshold_micros: number;
  amount_micros: number;
  payment_method_id: string | null;
}

export interface LastTopup {
  amount_micros: number;
  created_at?: string | null;
}

export interface BillingSummary {
  balance_micros: number;
  reserved_micros: number;
  warning: BalanceWarning | null;
  auto_recharge: AutoRecharge | null;
  last_topup: number | LastTopup | null;
}

export interface LedgerEntry {
  id: string;
  entry_type: "topup" | "usage" | "adjustment" | "refund" | "reserve" | "release";
  amount_micros: number;
  balance_after_micros: number;
  reference?: string | null;
  note?: string | null;
  created_at: string;
}

export interface LedgerPage {
  items: LedgerEntry[];
  next_cursor?: string | null;
}

export type UsageMetric =
  | "ai_voice_seconds"
  | "stt_seconds"
  | "tts_characters"
  | "llm_tokens_in"
  | "llm_tokens_out";

export interface UsageMetricLine {
  metric: string;
  quantity: number;
  price_micros: number;
}

export interface UsageCallRow {
  call_id: string;
  occurred_at?: string | null;
  price_micros: number;
  seconds?: number | null;
  contact?: string | null;
}

export interface UsageSummary {
  from?: string;
  to?: string;
  total_price_micros?: number;
  by_metric: UsageMetricLine[] | Record<string, { quantity: number; price_micros: number }>;
  calls?: UsageCallRow[];
}

export interface UsageEventRow {
  id?: string;
  metric: string;
  quantity: number;
  price_micros: number;
  occurred_at?: string | null;
}

export interface UsageCallDetail {
  call_id: string;
  total_price_micros?: number;
  events: UsageEventRow[];
  occurred_at?: string | null;
  seconds?: number | null;
}

export interface RateRow {
  metric: string;
  price_micros: number;
  unit?: string | null;
}

export interface PaymentMethodRow {
  id: string;
  brand: string;
  last4: string;
  is_default: boolean;
  exp_month?: number | null;
  exp_year?: number | null;
}

/**
 * Reuses spend's cent-rounding because credits are money too and must round the same way.
 */
export const formatCredits = formatMicros;

const unitPriceFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 6,
});

export function formatUnitPrice(micros: number): string {
  return unitPriceFormatter.format(micros / 1_000_000);
}

export function parseDollarsToMicros(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "" || !/^\d+(\.\d{1,2})?$/.test(trimmed)) return null;
  const value = Number(trimmed);
  // Zero is rejected as well as negatives: every caller (a top-up, an auto-recharge
  // threshold, an auto-recharge amount) is meaningless at $0, and the backend refuses a
  // non-positive top-up anyway - better to disable the button than to send a 422.
  if (!Number.isFinite(value) || value <= 0) return null;
  return dollarsToMicros(value);
}

export function lastTopupMicros(summary: BillingSummary | undefined): number {
  if (summary == null || summary.last_topup == null) return 0;
  if (typeof summary.last_topup === "number") return summary.last_topup;
  return summary.last_topup.amount_micros;
}

export const METRIC_LABELS: Record<string, string> = {
  ai_voice_seconds: "Assistant minutes",
  stt_seconds: "Speech recognition",
  tts_characters: "Voice generation",
  llm_tokens_in: "Language model input",
  llm_tokens_out: "Language model output",
};

export function metricLabel(metric: string): string {
  return METRIC_LABELS[metric] ?? metric.replace(/_/g, " ");
}

export function formatQuantity(metric: string, quantity: number): string {
  // The *_seconds metrics are metered in SECONDS (ai_usage_events.quantity) but nobody
  // buys seconds - the customer surface always says minutes, so convert here rather than
  // at each call site.
  if (metric === "ai_voice_seconds" || metric === "stt_seconds") {
    return `${(quantity / 60).toFixed(1)} minutes`;
  }
  if (metric === "tts_characters") {
    return `${quantity.toLocaleString("en-US")} characters`;
  }
  if (metric === "llm_tokens_in" || metric === "llm_tokens_out") {
    return `${quantity.toLocaleString("en-US")} tokens`;
  }
  return quantity.toLocaleString("en-US");
}

export function formatRateUnit(metric: string): string {
  if (metric === "ai_voice_seconds" || metric === "stt_seconds") return "per minute";
  if (metric === "tts_characters") return "per 1,000 characters";
  if (metric === "llm_tokens_in" || metric === "llm_tokens_out") return "per 1,000 tokens";
  return "per unit";
}

export function rateDisplayMicros(metric: string, priceMicros: number): number {
  if (metric === "ai_voice_seconds" || metric === "stt_seconds") {
    return Math.round(priceMicros * 60);
  }
  if (metric === "tts_characters" || metric === "llm_tokens_in" || metric === "llm_tokens_out") {
    return Math.round(priceMicros * 1_000);
  }
  return Math.round(priceMicros);
}

export const WARNING_COPY: Record<BalanceWarning, { title: string; body: string }> = {
  low: {
    title: "Your credits are running low",
    body: "Add credits so your assistant keeps answering.",
  },
  critical: {
    title: "You are almost out of credits",
    body: "Add credits now so your assistant keeps answering.",
  },
  empty: {
    title: "You are out of credits",
    body: "Your assistant is not answering and campaigns are paused until you add credits.",
  },
};

export const TOPUP_PRESETS_MICROS = [25_000_000, 50_000_000, 100_000_000] as const;

function withQuery(path: string, params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") search.set(key, String(value));
  });
  const qs = search.toString();
  return qs ? `${path}?${qs}` : path;
}

export async function fetchBillingSummary(api: ApiClient): Promise<BillingSummary> {
  return api.request<BillingSummary>(BILLING_SUMMARY_PATH);
}

export async function fetchLedgerPage(
  api: ApiClient,
  opts: { limit?: number; cursor?: string | null } = {},
): Promise<LedgerPage> {
  return api.request<LedgerPage>(
    withQuery(BILLING_LEDGER_PATH, { limit: opts.limit, cursor: opts.cursor ?? undefined }),
  );
}

export async function fetchUsageSummary(
  api: ApiClient,
  from: string,
  to: string,
): Promise<UsageSummary> {
  return api.request<UsageSummary>(withQuery(BILLING_USAGE_PATH, { from, to }));
}

export async function fetchUsageCall(api: ApiClient, callId: string): Promise<UsageCallDetail> {
  return api.request<UsageCallDetail>(`${BILLING_USAGE_CALL_PATH}/${encodeURIComponent(callId)}`);
}

export async function fetchRates(api: ApiClient): Promise<RateRow[]> {
  return api.request<RateRow[]>(BILLING_RATES_PATH);
}

export async function fetchPaymentMethods(api: ApiClient): Promise<PaymentMethodRow[]> {
  return api.request<PaymentMethodRow[]>(BILLING_PAYMENT_METHODS_PATH);
}

export async function createTopup(
  api: ApiClient,
  amountMicros: number,
): Promise<{ checkout_url: string }> {
  return api.request<{ checkout_url: string }>(BILLING_TOPUPS_PATH, {
    method: "POST",
    json: { amount_micros: amountMicros },
  });
}

export async function updateAutoRecharge(
  api: ApiClient,
  input: {
    enabled: boolean;
    threshold_micros: number;
    amount_micros: number;
    payment_method_id: string | null;
  },
): Promise<AutoRecharge> {
  return api.request<AutoRecharge>(BILLING_AUTO_RECHARGE_PATH, {
    method: "PATCH",
    json: input,
  });
}

export async function deletePaymentMethod(api: ApiClient, id: string): Promise<void> {
  await api.request<undefined>(`${BILLING_PAYMENT_METHODS_PATH}/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function addPaymentMethod(api: ApiClient): Promise<{ checkout_url: string }> {
  return api.request<{ checkout_url: string }>(BILLING_PAYMENT_METHODS_PATH, {
    method: "POST",
  });
}

export const BILLING_SUMMARY_KEY = ["billing", "summary"] as const;
export const BILLING_LEDGER_KEY = ["billing", "ledger"] as const;
export const BILLING_RATES_KEY = ["billing", "rates"] as const;
export const BILLING_PAYMENT_METHODS_KEY = ["billing", "payment-methods"] as const;

export function useBillingSummary(api: ApiClient, enabled = true) {
  return useQuery({
    queryKey: BILLING_SUMMARY_KEY,
    queryFn: () => fetchBillingSummary(api),
    enabled,
  });
}

export function useUsageSummary(api: ApiClient, from: string, to: string) {
  return useQuery({
    queryKey: ["billing", "usage", from, to],
    queryFn: () => fetchUsageSummary(api, from, to),
    enabled: Boolean(from && to),
  });
}

export function useRates(api: ApiClient) {
  return useQuery({
    queryKey: BILLING_RATES_KEY,
    queryFn: () => fetchRates(api),
  });
}

export function usePaymentMethods(api: ApiClient) {
  return useQuery({
    queryKey: BILLING_PAYMENT_METHODS_KEY,
    queryFn: () => fetchPaymentMethods(api),
  });
}

export function useLedger(api: ApiClient, cursor?: string | null) {
  return useQuery({
    queryKey: ["billing", "ledger", cursor ?? "first"],
    queryFn: () => fetchLedgerPage(api, { cursor: cursor ?? undefined }),
  });
}

export function useCreateTopup(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { amount_micros: number }) => createTopup(api, input.amount_micros),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["billing"], exact: false });
    },
  });
}

export function useUpdateAutoRecharge(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: {
      enabled: boolean;
      threshold_micros: number;
      amount_micros: number;
      payment_method_id: string | null;
    }) => updateAutoRecharge(api, input),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["billing"], exact: false });
    },
  });
}

export function useDeletePaymentMethod(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => deletePaymentMethod(api, id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["billing"], exact: false });
    },
  });
}

export function useAddPaymentMethod(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => addPaymentMethod(api),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["billing"], exact: false });
    },
  });
}

export function usageLines(summary: UsageSummary | undefined): UsageMetricLine[] {
  if (summary == null) return [];

  const raw = summary.by_metric;
  let lines: UsageMetricLine[];

  if (Array.isArray(raw)) {
    lines = raw.map((line) => ({ ...line }));
  } else {
    lines = Object.entries(raw).map(([metric, value]) => ({
      metric,
      quantity: value.quantity,
      price_micros: value.price_micros,
    }));
  }

  return lines
    .filter((line) => line.quantity !== 0 || line.price_micros !== 0)
    .sort((a, b) => b.price_micros - a.price_micros || a.metric.localeCompare(b.metric));
}

export function usageTotalMicros(summary: UsageSummary | undefined): number {
  if (summary == null) return 0;
  if (typeof summary.total_price_micros === "number") return summary.total_price_micros;
  return usageLines(summary).reduce((total, line) => total + line.price_micros, 0);
}

export function defaultCheckoutRedirect(url: string): void {
  window.location.assign(url);
}
