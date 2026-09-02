/**
 * Numbers domain (Phase 18 upgrade).
 *
 * `GET /numbers` and `GET /numbers/available` now return purchase/account bookkeeping
 * fields (provider_account_id/label, purchase/monthly cost cents, purchased_at,
 * order_detail, setup_cost_cents) that the generated `types.gen.ts` doesn't know about
 * yet - it's generated from the OpenAPI schema as it existed before this phase's backend
 * routes shipped. Intersecting with the generated types here (rather than hand-copying
 * every base field) keeps this correct once the schema is regenerated, per the phase-18
 * plan's instruction not to touch generated files or run gen:api early.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient } from "./client";
import type { components } from "./types.gen";

type GeneratedNumberOut = components["schemas"]["NumberOut"];
type GeneratedSearchOut = components["schemas"]["SearchOut"];

export type NumberStatus = "active" | "pending" | "failed" | "released";

export type NumberOut = Omit<GeneratedNumberOut, "status"> & {
  status: NumberStatus;
  provider_account_id: string | null;
  provider_account_label: string | null;
  purchase_cost_cents: number | null;
  monthly_cost_cents: number | null;
  purchased_at: string | null;
  order_detail: string | null;
};

export type SearchOut = GeneratedSearchOut & {
  monthly_cost_cents: number | null;
  setup_cost_cents: number | null;
};

export type AvailableNumberFilters = {
  carrier?: string;
  area_code?: string;
  contains?: string;
  locality?: string;
  region?: string;
  number_type?: string;
  limit?: number;
};

const NUMBERS_QUERY_KEY = ["numbers"] as const;
const PENDING_REFETCH_MS = 15000;

/**
 * Pure so it's directly unit-testable without mocking timers/visibility - mirrors
 * `isTerminalCallStatus` in api/hooks.ts.
 */
export function pendingRefetchInterval(numbers: NumberOut[] | undefined): number | false {
  return numbers?.some((n) => n.status === "pending") ? PENDING_REFETCH_MS : false;
}

export function useNumbers(api: ApiClient) {
  return useQuery({
    queryKey: NUMBERS_QUERY_KEY,
    queryFn: () => api.request<NumberOut[]>("/api/v1/numbers"),
    refetchInterval: (query) => {
      if (document.visibilityState !== "visible") return false;
      return pendingRefetchInterval(query.state.data);
    },
  });
}

export function useAvailableNumbers(
  api: ApiClient,
  filters: AvailableNumberFilters,
  enabled: boolean,
) {
  const params = new URLSearchParams();
  if (filters.carrier) params.set("carrier", filters.carrier);
  if (filters.area_code) params.set("area_code", filters.area_code);
  if (filters.contains) params.set("contains", filters.contains);
  if (filters.locality) params.set("locality", filters.locality);
  if (filters.region) params.set("region", filters.region);
  if (filters.number_type) params.set("number_type", filters.number_type);
  if (filters.limit != null) params.set("limit", String(filters.limit));
  const qs = params.toString();

  return useQuery({
    queryKey: ["numbers-available", filters],
    queryFn: () => api.request<SearchOut[]>(`/api/v1/numbers/available${qs ? `?${qs}` : ""}`),
    enabled,
    retry: false,
  });
}

/**
 * Numbers-domain order mutation. Reuses the ["numbers"] cache key so a successful order
 * invalidates the same list `useNumbers` (here and in api/hooks.ts) reads.
 *
 * Not the api/hooks.ts `useOrderNumber` - that one's vars type predates this phase's
 * optional cost fields, and hooks.ts is otherwise off-limits for this page.
 */
export function useOrderNumber(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      e164: string;
      carrier?: string;
      campaign_id?: string;
      monthly_cost_cents?: number;
      setup_cost_cents?: number;
    }) => api.request<NumberOut>("/api/v1/numbers/order", { method: "POST", json: vars }),
    onSuccess: () => qc.invalidateQueries({ queryKey: NUMBERS_QUERY_KEY }),
  });
}

export function formatMonthlyCost(item: {
  monthly_cost_cents?: number | null;
  monthly_cost?: string | null;
}): string {
  if (item.monthly_cost_cents != null) return `$${(item.monthly_cost_cents / 100).toFixed(2)}`;
  if (item.monthly_cost) return item.monthly_cost;
  return "—";
}

export function formatSetupCost(cents: number | null | undefined): string {
  return cents == null ? "—" : `$${(cents / 100).toFixed(2)}`;
}
