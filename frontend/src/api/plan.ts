import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "./client";
import type { ApiClient } from "./client";

/** Workspace plans: Solo / Team / Business plus $15 add-on users and $5 add-on numbers.
 *  Mirrors backend/app/services/plan_billing.py `summary()`. */
export const WORKSPACE_PLAN_PATH = "/api/v1/billing/plan";

export type PlanCode = "solo" | "team" | "business";

export interface CatalogPlan {
  code: PlanCode;
  name: string;
  users: number;
  numbers: number;
  price_cents: number;
  minutes: number;
  extra_user_cents?: number;
  max_users?: number | null;
  yearly_price_cents?: number;
  monthly_total_cents_if_switched: number;
  /** Per billing period of the current plan: what a plan change must accept. */
  total_cents_if_switched?: number;
}

export type BillingInterval = "month" | "year";

/** Months charged per bill: 1 monthly, 10 on a yearly plan (two months free). */
export function monthsPerBill(interval: BillingInterval | undefined, data?: { months_billed_per_year?: number }) {
  return interval === "year" ? data?.months_billed_per_year ?? 10 : 1;
}

/** A block of numbers bought at a discount to the $5 add-on price. `available` is false for a
 *  size this workspace cannot buy (e.g. any pack on a yearly plan). */
export interface NumberPack {
  code: "25" | "50" | "100";
  size: number;
  list_price_cents: number;
  price_cents: number;
  per_number_cents: number;
  owned: number;
  available: boolean;
}

export interface WorkspacePlan {
  plan: null | {
    code: PlanCode;
    name: string;
    status: string;
    price_cents: number;
    monthly_total_cents: number;
    interval?: BillingInterval;
    period_total_cents?: number;
    renews_at: string | null;
    cancel_at_period_end: boolean;
  };
  users: { limit: number | null; in_use: number; included?: number; extra?: number };
  numbers: { limit: number | null; in_use: number; included?: number; extra?: number; in_packs?: number };
  minutes?: { included: number; remaining: number };
  extra_user_cents: number;
  extra_number_cents: number;
  minutes_per_user?: number;
  yearly_available?: boolean;
  months_billed_per_year?: number;
  number_packs?: NumberPack[];
  catalog: CatalogPlan[];
}

/** The API's refusal when a change costs more than the caller confirmed. */
export interface PriceQuote {
  monthly_increase_cents?: number;
  monthly_total_cents?: number;
}

export const dollars = (cents: number) =>
  `$${(cents / 100).toFixed(cents % 100 === 0 ? 0 : 2)}`;

/** What buying this plan outright saves against Solo plus add-ons for the same people. */
export function planSaving(plan: CatalogPlan, extraUserCents = 1500, extraNumberCents = 500) {
  const solo = 1500 + (plan.users - 1) * extraUserCents + (plan.numbers - 1) * extraNumberCents;
  return Math.max(solo - plan.price_cents, 0);
}

export function useWorkspacePlan(api: ApiClient, enabled = true) {
  return useQuery({
    queryKey: ["billing", "plan"],
    queryFn: () => api.request<WorkspacePlan>(WORKSPACE_PLAN_PATH),
    enabled,
  });
}

function usePlanMutation<V>(api: ApiClient, path: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (json: V) =>
      api.request<WorkspacePlan>(`${WORKSPACE_PLAN_PATH}${path}`, { method: "POST", json }),
    onSuccess: (data) => {
      qc.setQueryData(["billing", "plan"], data);
      void qc.invalidateQueries({ queryKey: ["org-seats"] });
      void qc.invalidateQueries({ queryKey: ["billing"] });
    },
  });
}

export const useAddPlanUsers = (api: ApiClient) =>
  usePlanMutation<{ count: number; accept_cents: number }>(api, "/users");

export const useChangePlan = (api: ApiClient) =>
  usePlanMutation<{ plan_code: PlanCode; accept_cents: number }>(api, "/change");

export const useTrimPlan = (api: ApiClient) => usePlanMutation<Record<string, never>>(api, "/trim");

/** `accept_cents: null` asks for the quote; the API refuses with the exact change to accept. */
export const useBuyPack = (api: ApiClient) =>
  usePlanMutation<{ code: string; accept_cents: number | null }>(api, "/packs");

export function useRemovePack(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (code: string) =>
      api.request<WorkspacePlan>(`${WORKSPACE_PLAN_PATH}/packs/${code}`, { method: "DELETE" }),
    onSuccess: (data) => {
      qc.setQueryData(["billing", "plan"], data);
      void qc.invalidateQueries({ queryKey: ["org-seats"] });
      void qc.invalidateQueries({ queryKey: ["billing"] });
    },
  });
}

/** The change to this billing period's bill the API wants accepted, or null when the failure
 *  was something else. Negative means the pack replaces add-on numbers and the bill drops. */
export function packQuote(err: unknown): number | null {
  if (!(err instanceof ApiError) || err.code !== "price_confirmation_required") return null;
  const cents = (err.details as { quote?: { monthly_increase_cents?: unknown } } | null | undefined)?.quote
    ?.monthly_increase_cents;
  return typeof cents === "number" ? cents : null;
}
