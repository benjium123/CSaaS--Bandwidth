/**
 * Identity and security domain (P25).
 *
 * The backend is authoritative for session/login-event/security-policy data; this module
 * only exposes typed queries/mutations plus the two 422 error codes the UI must attach
 * to the right form fields.
 *
 * Dates arrive as ISO strings from the API, so they are typed as `string` throughout.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, fetchAuthedBlob, type ApiClient } from "./client";
import { downloadTextFile } from "./contactsPro";

export type SessionOut = {
  id: string;
  ip: string | null;
  user_agent: string | null;
  last_seen_at: string | null;
  created_at: string;
  expires_at: string;
  current: boolean;
};

export type LoginEventOut = {
  id: string;
  at: string;
  ip: string | null;
  user_agent: string | null;
  outcome: string;
  detail: string | null;
  email: string;
  user_id: string | null;
};

export type SsoConfigOut = {
  issuer: string;
  client_id: string;
  domain: string;
  enforce: boolean;
  default_role_id: string | null;
  client_secret_set: boolean;
};

export type SecurityPolicyOut = {
  require_2fa: boolean;
  require_2fa_grace_until: string | null;
  ip_allowlist: string[] | null;
  sso: SsoConfigOut | null;
};

export type SsoConfigIn = {
  issuer?: string;
  client_id?: string;
  client_secret?: string;
  domain?: string;
  enforce?: boolean;
  default_role_id?: string | null;
};

export type SecurityPolicyIn = {
  require_2fa?: boolean;
  ip_allowlist?: string[] | null;
  sso?: SsoConfigIn | null;
};

export const SESSIONS_QUERY_KEY = ["me", "sessions"] as const;
export const MY_LOGIN_EVENTS_QUERY_KEY = ["me", "login-events"] as const;
export const ORG_LOGIN_EVENTS_QUERY_KEY = ["org", "login-events"] as const;
export const SECURITY_POLICY_QUERY_KEY = ["org", "current", "security"] as const;

export function useSessions(api: ApiClient) {
  return useQuery({
    queryKey: SESSIONS_QUERY_KEY,
    queryFn: () => api.request<SessionOut[]>("/api/v1/me/sessions"),
  });
}

export function useRevokeSession(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (sid: string) =>
      api.request<void>(`/api/v1/me/sessions/${sid}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY });
    },
  });
}

export function useRevokeAllSessions(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api.request<{ revoked: number }>("/api/v1/me/sessions/revoke-all", {
        method: "POST",
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: SESSIONS_QUERY_KEY });
    },
  });
}

export function useMyLoginEvents(api: ApiClient, limit = 50) {
  return useQuery({
    queryKey: [...MY_LOGIN_EVENTS_QUERY_KEY, limit],
    queryFn: () => api.request<LoginEventOut[]>(`/api/v1/me/login-events?limit=${limit}`),
  });
}

export function useOrgLoginEvents(
  api: ApiClient,
  params: { limit?: number; outcome?: string | null; enabled?: boolean },
) {
  const limit = params.limit ?? 50;
  const outcome = params.outcome ?? null;

  return useQuery({
    enabled: params.enabled ?? true,
    // `outcome` is deliberately NOT in the key: the backend has no outcome parameter, so the
    // filter is applied in `select` below. Keying on it would give each filter its own cache
    // entry and refetch the whole list every time the dropdown moves.
    queryKey: [...ORG_LOGIN_EVENTS_QUERY_KEY, limit],
    queryFn: () =>
      api.request<LoginEventOut[]>(`/api/v1/orgs/current/login-events?limit=${limit}`),
    select: (rows) =>
      outcome != null && outcome !== ""
        ? rows.filter((row) => row.outcome === outcome)
        : rows,
  });
}

export function useSecurityPolicy(api: ApiClient, enabled = true) {
  return useQuery({
    queryKey: SECURITY_POLICY_QUERY_KEY,
    queryFn: () => api.request<SecurityPolicyOut>("/api/v1/orgs/current/security"),
    enabled,
    retry: false,
  });
}

export function useUpdateSecurityPolicy(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: SecurityPolicyIn) =>
      api.request<SecurityPolicyOut>("/api/v1/orgs/current/security", {
        method: "PATCH",
        json: body,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: SECURITY_POLICY_QUERY_KEY });
    },
  });
}

export const SECURITY_POLICY_ERROR_CODES = {
  TWO_FACTOR_REQUIRED_FOR_ACTOR: "two_factor_required_for_actor",
  IP_ALLOWLIST_WOULD_LOCK_YOU_OUT: "ip_allowlist_would_lock_you_out",
} as const;

/** The code of a 422 only. The panels use it to decide WHICH field the inline error
 * belongs to; a 403/500 must never light up a field. */
export function securityPolicyErrorCode(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 422) return null;
  return err.code;
}

export function securityPolicyErrorMessage(err: unknown): string {
  switch (securityPolicyErrorCode(err)) {
    case SECURITY_POLICY_ERROR_CODES.TWO_FACTOR_REQUIRED_FOR_ACTOR:
      return "Set up two-factor authentication on your own account before you can require it for everyone in this workspace.";
    case SECURITY_POLICY_ERROR_CODES.IP_ALLOWLIST_WOULD_LOCK_YOU_OUT:
      return "Your current IP address is outside the ranges you are saving. Add a range that includes it, or you will lock yourself out.";
    default:
      return err instanceof Error ? err.message : "Something went wrong.";
  }
}

export async function downloadOrgLoginEventsCsv(
  api: ApiClient,
  limit = 200,
): Promise<void> {
  const blob = await fetchAuthedBlob(
    api,
    `/api/v1/orgs/current/login-events?limit=${limit}&format=csv`,
  );
  const text = await blob.text();
  downloadTextFile("login-events.csv", text, "text/csv");
}

export function loginOutcomeLabel(outcome: string): string {
  switch (outcome) {
    case "ok":
      return "Signed in";
    case "bad_password":
      return "Wrong password";
    case "bad_2fa":
      return "Wrong 2FA code";
    case "locked":
      return "Account locked";
    case "blocked_ip":
      return "Blocked by IP allowlist";
    case "sso":
      return "Signed in with SSO";
    default:
      return outcome;
  }
}

export function loginOutcomeTone(
  outcome: string,
): "success" | "danger" | "warning" | "neutral" {
  switch (outcome) {
    case "ok":
    case "sso":
      return "success";
    case "bad_password":
    case "bad_2fa":
      return "warning";
    case "locked":
    case "blocked_ip":
      return "danger";
    default:
      return "neutral";
  }
}
