/**
 * Per-workspace discounts for the ops console. Backend: the ops console discounts routes.
 *
 * A discount is a percentage off one or more charge categories (subscription, bundles, usage,
 * numbers, the 10DLC fee). The server owns the authoritative list, so we never fake a change
 * locally: every write is a PUT/DELETE and the list is refetched. A write also invalidates the
 * console orgs list (see @/api/opsConsole), since what a workspace pays changes. Writes need an
 * admin operator; a reviewer gets a 403 that we surface verbatim rather than pretending the
 * change stuck.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient } from "./client";

export interface Discount {
  category: string;
  percent_bps: number;
  percent: number;
  ends_at: string | null;
  active: boolean;
  note: string | null;
  stripe_coupon_id: string | null;
  updated_at: string | null;
}

export type StripeSyncStatus = "synced" | "no_subscription" | "failed" | null;

export interface OrgDiscountsData {
  org_id: string;
  categories: string[];
  discounts: Discount[];
}

/** PUT/DELETE return the same shape as GET plus how the Stripe side went. */
export interface OrgDiscountsWriteResult extends OrgDiscountsData {
  stripe: StripeSyncStatus;
}

export interface SetOrgDiscountsBody {
  categories: string[];
  percent: number;
  ends_at: string | null;
  note: string | null;
}

/** Operator-facing label + one-line help for each charge category. A category the backend adds
 * that we do not know yet falls back to its raw key and an empty help line rather than being
 * hidden, so a new charge is still discountable from day one. */
export const DISCOUNT_LABELS: Record<string, { label: string; help: string }> = {
  subscription: {
    label: "Subscription",
    help: "Plan and extra users (Stripe coupon, from the next invoice)",
  },
  bundles: {
    label: "Bundles",
    help: "SMS, MMS and call-minute bundles, after the volume discount",
  },
  usage: {
    label: "Usage",
    help: "Per text, per minute, fax and AI usage charged from the balance",
  },
  numbers: {
    label: "Phone numbers",
    help: "Number rental and setup (and extra numbers on the plan)",
  },
  tendlc: {
    label: "10DLC fee",
    help: "Ringlite's texting-registration service fee (carrier fees stay at cost)",
  },
};

/** Label/help for a category key, with a passthrough for a category this bundle does not know. */
export function discountLabel(category: string): { label: string; help: string } {
  return DISCOUNT_LABELS[category] ?? { label: category, help: "" };
}

function discountsPath(orgId: string): string {
  return `/api/v1/ops/console/orgs/${orgId}/discounts`;
}

// ---------------------------------------------------------------------------------------
// Fetchers
// ---------------------------------------------------------------------------------------

export async function fetchOrgDiscounts(
  api: ApiClient,
  orgId: string,
): Promise<OrgDiscountsData> {
  const data = await api.request<OrgDiscountsData>(discountsPath(orgId));
  return {
    org_id: data.org_id ?? orgId,
    categories: data.categories ?? [],
    discounts: data.discounts ?? [],
  };
}

export async function setOrgDiscounts(
  api: ApiClient,
  orgId: string,
  body: SetOrgDiscountsBody,
): Promise<OrgDiscountsWriteResult> {
  return api.request<OrgDiscountsWriteResult>(discountsPath(orgId), {
    method: "PUT",
    json: body,
  });
}

export async function removeOrgDiscount(
  api: ApiClient,
  orgId: string,
  category: string,
): Promise<OrgDiscountsWriteResult> {
  return api.request<OrgDiscountsWriteResult>(`${discountsPath(orgId)}/${category}`, {
    method: "DELETE",
  });
}

// ---------------------------------------------------------------------------------------
// react-query hooks
// ---------------------------------------------------------------------------------------

/** Root of the query keys used by the console hooks in @/api/opsConsole (useConsoleOrgs et al).
 * Invalidating it refetches this list *and* the org rows rendered by ConsoleTab, since a
 * discount changes what a workspace is charged. */
const CONSOLE_QUERY_ROOT = ["ops", "console"] as const;

export function orgDiscountsQueryKey(orgId: string) {
  return [...CONSOLE_QUERY_ROOT, "org-discounts", orgId] as const;
}

function invalidateDiscounts(qc: ReturnType<typeof useQueryClient>, orgId: string) {
  void qc.invalidateQueries({ queryKey: orgDiscountsQueryKey(orgId) });
  void qc.invalidateQueries({ queryKey: CONSOLE_QUERY_ROOT });
}

export function useOrgDiscounts(api: ApiClient, orgId: string) {
  return useQuery({
    queryKey: orgDiscountsQueryKey(orgId),
    queryFn: () => fetchOrgDiscounts(api, orgId),
  });
}

export function useSetOrgDiscounts(api: ApiClient, orgId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SetOrgDiscountsBody) => setOrgDiscounts(api, orgId, body),
    onSuccess: () => invalidateDiscounts(qc, orgId),
  });
}

export function useRemoveOrgDiscount(api: ApiClient, orgId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (category: string) => removeOrgDiscount(api, orgId, category),
    onSuccess: () => invalidateDiscounts(qc, orgId),
  });
}

// ---------------------------------------------------------------------------------------
// Small display helpers
// ---------------------------------------------------------------------------------------

/** Percent from the integer basis points the backend stores, with up to 2 decimals and no
 * trailing zeros: 1250 -> "12.5", 1000 -> "10", 10 -> "0.1". */
export function formatDiscountPercent(percentBps: number): string {
  return (percentBps / 100).toFixed(2).replace(/\.?0+$/, "") || "0";
}
