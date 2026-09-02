import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient } from "./client";

export const SPEND_METRICS = [
  "sms_out",
  "sms_in",
  "mms_out",
  "mms_in",
  "voice_min_out",
  "voice_min_in",
  "number_mrc",
  "number_setup",
] as const;

export type SpendMetric = (typeof SPEND_METRICS)[number];

export interface SpendMetricLine {
  quantity: number;
  cost_micros: number;
}

export interface SpendNumberLine {
  number_id: string;
  e164: string;
  cost_micros: number;
}

export interface ProviderSpendSummary {
  cost_micros: number;
  by_metric: Partial<Record<SpendMetric, SpendMetricLine>>;
  numbers: SpendNumberLine[];
}

export interface SpendSummary {
  total_micros: number;
  total_usd: string;
  by_provider: Record<string, ProviderSpendSummary>;
  // Carriers with spend rows this period but no resolvable rate card (no org override AND
  // no code-level default) - their cost_micros is 0, not "genuinely free". Optional so an
  // older backend response (pre-dating this field) still type-checks.
  unrated_providers?: string[];
}

export interface SpendDailyRow {
  period_date: string;
  provider: string;
  metric: SpendMetric;
  quantity: number;
  cost_micros: number;
}

export interface ProviderRate {
  provider: string;
  metric: SpendMetric;
  unit_cost_micros: number;
  // The backend's code-level DEFAULT_RATES value for this (provider, metric), in micros -
  // what Reset writes back, whether or not this row is currently an override. Replaces the
  // old frontend-hardcoded DEFAULT_RATE_USD table, which could drift from the backend.
  default_unit_cost_micros: number;
  is_override: boolean;
  currency: string;
}

export interface UpdateRateInput {
  provider: string;
  metric: SpendMetric;
  unit_cost_micros: number;
}

export interface RollupResponse {
  day: string;
  detail?: string;
}

const currencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
});

/**
 * Formats integer micros as a signed USD string ("$1.24", "-$1.23", "$12,345.68").
 *
 * Rounds at the CENT boundary via integer division (micros per cent = 1_000_000 / 100 =
 * 10_000), not by calling .toFixed(2) on the raw dollar float: `(1_005_000 /
 * 1_000_000).toFixed(2)` gives "$1.00", not "$1.01", because 1.005 is stored as
 * 1.00499999999999989... in IEEE754, not 1.005 - .toFixed(2) truncates that down instead
 * of rounding up. Math.round on the micros-to-cents division rounds the true integer
 * value instead of a lossy float.
 */
export function formatMicros(micros: number): string {
  const cents = Math.round(micros / 10_000);
  return currencyFormatter.format(cents / 100);
}

export function microsToDollars(micros: number): number {
  return micros / 1_000_000;
}

export function dollarsToMicros(dollars: number): number {
  return Math.round(dollars * 1_000_000);
}

/** Guards `(err as Error).message` against a non-Error rejection (a thrown string, a plain
 * object, etc.) so a mutation's error UI never renders "undefined". */
export function getErrorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === "string" && err.trim() !== "") return err;
  return "Something went wrong.";
}

/** UTC calendar day (YYYY-MM-DD) for "today", used both to label the MTD figures and as
 * the `day` query param for a manual rollup. */
export function todayUTC(now = new Date()): string {
  return now.toISOString().slice(0, 10);
}

export function formatDateShort(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

export function monthToDateRange(now = new Date()): { from: string; to: string } {
  const fmt = (d: Date) => d.toISOString().slice(0, 10);
  return {
    from: fmt(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1))),
    to: fmt(now),
  };
}

export function lastNDaysRange(days: number, now = new Date()): { from: string; to: string } {
  const to = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  const from = new Date(to);
  from.setUTCDate(from.getUTCDate() - (days - 1));
  return { from: from.toISOString().slice(0, 10), to: to.toISOString().slice(0, 10) };
}

/** Every UTC calendar day in [from, to] inclusive, as YYYY-MM-DD - used to zero-fill a
 * daily series so a day with no spend rows still renders its own (zero-height) bar. */
export function daysInRange(from: string, to: string): string[] {
  const start = new Date(`${from}T00:00:00Z`);
  const end = new Date(`${to}T00:00:00Z`);
  const days: string[] = [];
  for (let d = start; d.getTime() <= end.getTime(); d = new Date(d.getTime() + 86_400_000)) {
    days.push(d.toISOString().slice(0, 10));
  }
  return days;
}

function encodeSearchParams(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== "") {
      search.set(key, String(value));
    }
  });
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

export async function fetchSpendSummary(
  api: ApiClient,
  from: string,
  to: string,
): Promise<SpendSummary> {
  return api.request<SpendSummary>(
    `/api/v1/spend/summary${encodeSearchParams({ from, to })}`,
  );
}

export async function fetchSpendDaily(
  api: ApiClient,
  from: string,
  to: string,
  provider?: string | null,
): Promise<SpendDailyRow[]> {
  return api.request<SpendDailyRow[]>(
    `/api/v1/spend/daily${encodeSearchParams({ from, to, provider: provider ?? undefined })}`,
  );
}

export async function fetchProviderRates(api: ApiClient): Promise<ProviderRate[]> {
  return api.request<ProviderRate[]>("/api/v1/provider-rates");
}

export async function updateProviderRates(
  api: ApiClient,
  rates: UpdateRateInput[],
): Promise<ProviderRate[]> {
  return api.request<ProviderRate[]>("/api/v1/provider-rates", {
    method: "PUT",
    json: { rates },
  });
}

export async function rollupSpendDay(
  api: ApiClient,
  day: string,
): Promise<RollupResponse> {
  return api.request<RollupResponse>(
    `/api/v1/spend/rollup${encodeSearchParams({ day })}`,
    { method: "POST" },
  );
}

export function useSpendSummary(api: ApiClient, from: string, to: string) {
  return useQuery({
    queryKey: ["spend", "summary", from, to],
    queryFn: () => fetchSpendSummary(api, from, to),
    enabled: Boolean(from && to),
  });
}

export function useSpendDaily(
  api: ApiClient,
  from: string,
  to: string,
  provider?: string | null,
) {
  return useQuery({
    queryKey: ["spend", "daily", from, to, provider ?? "all"],
    queryFn: () => fetchSpendDaily(api, from, to, provider),
    enabled: Boolean(from && to),
  });
}

export function useProviderRates(api: ApiClient) {
  return useQuery({
    queryKey: ["spend", "provider-rates"],
    queryFn: () => fetchProviderRates(api),
  });
}

export function useUpdateRates(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (rates: UpdateRateInput[]) => updateProviderRates(api, rates),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["spend"], exact: false });
    },
  });
}

export function useRollupDay(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (day: string) => rollupSpendDay(api, day),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["spend"], exact: false });
    },
  });
}
