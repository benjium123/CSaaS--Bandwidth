/**
 * Capabilities-backed nav gating.
 *
 * The backend remains the authority; this gate only decides what to RENDER, never what
 * is allowed.
 */
import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import type { ApiClient } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";

export type RegistrationState = "none" | "pending" | "approved";

export type AccountType = "business" | "individual";

/**
 * Backend-reported onboarding progression. `ready` is the terminal state; anything
 * unrecognised is treated as not-ready (see `isOnboardingStep`).
 */
export type OnboardingStep =
  | "verification"
  | "awaiting_review"
  | "remediation"
  | "numbers"
  | "ready";

/**
 * Guards untrusted wire values: a missing or unknown step must fail CLOSED (not ready),
 * so never cast `onboarding_step` to the union without checking it first.
 */
export function isOnboardingStep(value: unknown): value is OnboardingStep {
  return (
    value === "verification" ||
    value === "awaiting_review" ||
    value === "remediation" ||
    value === "numbers" ||
    value === "ready"
  );
}

export type OrgCapabilities = {
  has_provider: boolean;
  has_number: boolean;
  member_count: number;
  registration_state: RegistrationState | string;
  /** Optional: older backends omit it; treat missing as "business". */
  account_type?: AccountType;
  /** Free-form KYC status reported by the backend ("unknown" when it omits one). */
  kyc_status: string;
  /** Current onboarding step; validate wire values with `isOnboardingStep`. */
  onboarding_step: OnboardingStep;
  calling_ready: boolean;
  messaging_ready: boolean;
};

export type Capabilities = {
  permissions: string[];
  org: OrgCapabilities;
  /** Per-workspace feature switches (backend services/entitlements.py): key -> on/off.
   * Optional so an older API without it still type-checks; treat missing as "ask the server". */
  features?: Record<string, boolean>;
};

export const CAPABILITIES_QUERY_KEY = ["me", "capabilities"] as const;

/** Individual accounts text only once their own 10DLC registration is approved (a hard
 * gate; the server refuses too, backend compliance/registration.py). Unknown capabilities
 * leave texting on: the server stays the authority. */
export function textingLocked(org: OrgCapabilities | null | undefined): boolean {
  return org?.account_type === "individual" && !org.messaging_ready;
}

export const TEXTING_LOCKED_NOTE =
  "Texting turns on once your 10DLC registration is approved (Settings → Messaging).";

export function useCapabilities(api: ApiClient) {
  return useQuery({
    queryKey: CAPABILITIES_QUERY_KEY,
    queryFn: () => api.request<Capabilities>("/api/v1/me/capabilities"),
    retry: false,
    staleTime: 30_000,
    refetchInterval: (query) => ["awaiting_review", "numbers", "remediation"].includes(query.state.data?.org.onboarding_step ?? "") ? 5_000 : false,
  });
}

export type Gate = {
  can: (permission: string) => boolean;
  org: OrgCapabilities | null;
  isLoading: boolean;
  source: "capabilities" | "membership" | "unknown";
};

export function useGate(): Gate {
  const { api, me, orgId } = useAuth();
  const capabilitiesQuery = useCapabilities(api);
  const data = capabilitiesQuery.data;

  const can = React.useCallback(
    (permission: string) => {
      if (capabilitiesQuery.isLoading) return false;
      if (capabilitiesQuery.error || data == null) {
        // B2 (Opus P20a verify): MembershipOut has no `permissions`; the backend sends them
        // top-level on /auth/me. Fail CLOSED when even that is missing - never show an
        // admin item to an agent because a lookup came back undefined.
        return me?.permissions?.includes(permission) ?? false;
      }
      return data.permissions.includes(permission);
    },
    [capabilitiesQuery.isLoading, capabilitiesQuery.error, data, me, orgId],
  );

  return React.useMemo<Gate>(() => {
    if (capabilitiesQuery.isLoading) {
      return { can, org: null, isLoading: true, source: "unknown" };
    }

    if (capabilitiesQuery.error || data == null) {
      return { can, org: null, isLoading: false, source: "membership" };
    }

    // Merge the selected membership's account_type into the org when the server omits it,
    // so downstream presentation (checklist, settings, ops) sees a consistent value.
    const membership = me?.memberships.find((m) => m.org_id === orgId);
    const org: OrgCapabilities = {
      ...data.org,
      account_type: data.org.account_type ?? membership?.account_type ?? "business",
    };

    return {
      can,
      org,
      isLoading: false,
      source: "capabilities",
    };
  }, [capabilitiesQuery.isLoading, capabilitiesQuery.error, data, can, me, orgId]);
}
