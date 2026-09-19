import { useMutation, useQuery } from "@tanstack/react-query";
import type { ApiClient } from "@/api/client";

export const BILLING_PLANS_PATH = "/api/v1/billing/plans";
export const BILLING_CHECKOUT_PATH = "/api/v1/billing/subscription/checkout";
export const BILLING_PLANS_KEY = ["billing", "plans"] as const;

export type Plan = {
  code: string;
  name: string;
  included: Record<string, number>;
  overage_rates: Record<string, number>;
  monthly_price_micros: number;
  stripe_price_id: string | null;
  is_active: boolean;
};

export const INCLUDED_LABELS: Record<string, string> = {
  sms_segments: "SMS segments",
  // The seeded plans use `voice_minutes` and `numbers`; these two were missing, so they
  // fell through to the `key.replace(/_/g, " ")` path and rendered lowercase next to
  // properly-cased siblings. The fallback is still correct for an operator-added metric -
  // it must never drop an allowance it has no label for - but a metric we ship should be
  // spelled properly here.
  voice_minutes: "Voice minutes",
  numbers: "Phone numbers",
  // Kept for operator-defined plans that may use these spellings.
  mms_messages: "MMS messages",
  call_minutes: "Call minutes",
  phone_numbers: "Phone numbers",
  seats: "Seats",
};

export function isPurchasable(plan: Plan): boolean {
  return plan.is_active && plan.stripe_price_id !== null;
}

export function includedLines(plan: Plan): { key: string; label: string; quantity: number }[] {
  return Object.entries(plan.included)
    .map(([key, quantity]) => ({
      key,
      label: INCLUDED_LABELS[key] ?? key.replace(/_/g, " "),
      quantity,
    }))
    .sort((a, b) => a.key.localeCompare(b.key));
}

export async function fetchPlans(api: ApiClient): Promise<Plan[]> {
  return api.request<Plan[]>(BILLING_PLANS_PATH);
}

export function usePlans(api: ApiClient, enabled = true) {
  return useQuery({
    queryKey: BILLING_PLANS_KEY,
    queryFn: () => fetchPlans(api),
    enabled,
  });
}

export async function createSubscriptionCheckout(
  api: ApiClient,
  code: string,
): Promise<{ checkout_url: string }> {
  return api.request<{ checkout_url: string }>(BILLING_CHECKOUT_PATH, {
    method: "POST",
    json: { plan_code: code },
  });
}

export function useSubscriptionCheckout(
  api: ApiClient,
  onCheckout: (url: string) => void,
) {
  return useMutation({
    mutationFn: (input: { plan_code: string }) => createSubscriptionCheckout(api, input.plan_code),
    onSuccess: (data) => onCheckout(data.checkout_url),
  });
}
