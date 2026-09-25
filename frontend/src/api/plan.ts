import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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
  monthly_total_cents_if_switched: number;
}

export interface WorkspacePlan {
  plan: null | {
    code: PlanCode;
    name: string;
    status: string;
    price_cents: number;
    monthly_total_cents: number;
    renews_at: string | null;
    cancel_at_period_end: boolean;
  };
  users: { limit: number | null; in_use: number; included?: number; extra?: number };
  numbers: { limit: number | null; in_use: number; included?: number; extra?: number };
  minutes?: { included: number; remaining: number };
  extra_user_cents: number;
  extra_number_cents: number;
  minutes_per_user: number;
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
