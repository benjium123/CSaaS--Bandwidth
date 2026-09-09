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

export type OrgCapabilities = {
  has_provider: boolean;
  has_number: boolean;
  member_count: number;
  registration_state: RegistrationState | string;
};

export type Capabilities = {
  permissions: string[];
  org: OrgCapabilities;
};

export const CAPABILITIES_QUERY_KEY = ["me", "capabilities"] as const;

export function useCapabilities(api: ApiClient) {
  return useQuery({
    queryKey: CAPABILITIES_QUERY_KEY,
    queryFn: () => api.request<Capabilities>("/api/v1/me/capabilities"),
    retry: false,
    staleTime: 30_000,
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

    return {
      can,
      org: data.org,
      isLoading: false,
      source: "capabilities",
    };
  }, [capabilitiesQuery.isLoading, capabilitiesQuery.error, data, can]);
}
