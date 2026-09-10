import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { createClient, type ApiClient } from "@/api/client";

export type Membership = {
  org_id: string;
  org_name: string;
  org_slug: string;
  role_name: string;
  /** P20 RBAC: effective permission strings for this membership. Optional - rolls out
   * server-side independently of this file, so every reader must feature-detect
   * (undefined = backend hasn't shipped it yet for this membership = don't restrict). */
  permissions?: string[];
};

/** Feature-detected permission gate (P20): `undefined` permissions means the backend
 * hasn't started sending them for this membership yet - fail OPEN (don't restrict)
 * rather than lock users out of actions they've always had. Once `permissions` is
 * present, it is authoritative. */
export function hasPermission(me: Me | null, orgId: string | null, permission: string): boolean {
  if (!me || !orgId) return true;
  const membership = me.memberships.find((m) => m.org_id === orgId);
  if (!membership || !membership.permissions) return true;
  return membership.permissions.includes(permission);
}

export type Me = {
  /** Effective permissions of the current membership (top-level on /auth/me). */
  permissions?: string[];
  id: string;
  email: string;
  full_name: string;
  memberships: Membership[];
  /** Item 8: rolling out server-side in a parallel batch - optional so this client keeps
   * working against a backend that doesn't send it yet. Treat undefined as false (2FA
   * not enabled) rather than guessing either way. */
  totp_enabled?: boolean;
};

type LoginResult =
  | { kind: "ok" }
  | { kind: "needs_2fa"; pendingToken: string }
  | { kind: "error"; message: string };

type AuthValue = {
  api: ApiClient;
  me: Me | null;
  orgId: string | null;
  ready: boolean;
  login(email: string, password: string): Promise<LoginResult>;
  verify2fa(pendingToken: string, code: string): Promise<LoginResult>;
  completeSso(accessToken: string, orgId: string): Promise<LoginResult>;
  selectOrg(orgId: string): void;
  logout(): void;
};

const AuthContext = React.createContext<AuthValue | null>(null);

export function useAuth(): AuthValue {
  const ctx = React.useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

export function AuthProvider({
  children,
  client,
}: {
  children: React.ReactNode;
  client?: ApiClient;
}) {
  const api = React.useMemo(() => client ?? createClient(), [client]);
  const queryClient = useQueryClient();
  const [me, setMe] = React.useState<Me | null>(null);
  const [orgId, setOrgId] = React.useState<string | null>(api.auth.orgId);
  const [ready, setReady] = React.useState(false);

  // P20: every identity/tenant boundary crossing (logout, forced-logout, org switch)
  // must drop every cached query - otherwise the next screen can render with another
  // user's or another org's stale cached data for a beat (or permanently, for a query
  // whose key doesn't vary by org).
  const logout = React.useCallback(() => {
    api.setAuth({ token: null, orgId: null });
    setMe(null);
    setOrgId(null);
    queryClient.clear();
  }, [api, queryClient]);

  // Item 1: onUnauthorized (fired on both a REST 401 and a WS 4401 close) must fully log
  // out - identical to a manual logout() - so it also drops the stored API token/orgId,
  // not just the in-memory me/orgId/query-cache. Reusing `logout` keeps both paths from
  // ever drifting apart again.
  React.useEffect(() => {
    api.onUnauthorized = logout;
  }, [api, logout]);

  const loadMe = React.useCallback(async () => {
    try {
      const next = await api.request<Me>("/api/v1/auth/me");
      setMe(next);
      // If the stored org is no longer one of ours, drop it rather than 403 on every call.
      if (orgId && !next.memberships.some((m) => m.org_id === orgId)) {
        api.setAuth({ orgId: null });
        setOrgId(null);
      }
      return next;
    } catch {
      setMe(null);
      return null;
    }
  }, [api, orgId]);

  React.useEffect(() => {
    (async () => {
      if (api.auth.token) await loadMe();
      setReady(true);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const login = React.useCallback(
    async (email: string, password: string): Promise<LoginResult> => {
      try {
        const res = await api.request<{
          access_token: string | null;
          requires_2fa: boolean;
          pending_token: string | null;
        }>("/api/v1/auth/login", { method: "POST", json: { email, password } });

        if (res.requires_2fa && res.pending_token) {
          return { kind: "needs_2fa", pendingToken: res.pending_token };
        }
        api.setAuth({ token: res.access_token });
        await loadMe();
        return { kind: "ok" };
      } catch (err) {
        return { kind: "error", message: (err as Error).message };
      }
    },
    [api, loadMe],
  );

  const verify2fa = React.useCallback(
    async (pendingToken: string, code: string): Promise<LoginResult> => {
      try {
        const res = await api.request<{ access_token: string }>("/api/v1/auth/2fa/verify", {
          method: "POST",
          json: { pending_token: pendingToken, code },
        });
        api.setAuth({ token: res.access_token });
        await loadMe();
        return { kind: "ok" };
      } catch (err) {
        return { kind: "error", message: (err as Error).message };
      }
    },
    [api, loadMe],
  );

  const completeSso = React.useCallback(
    async (accessToken: string, orgId: string): Promise<LoginResult> => {
      try {
        // WHY: the SSO callback returns the token as JSON rather than redirecting, so
        // the console completes the login itself; setting the org here as well means
        // an SSO user lands straight in their workspace instead of the org picker.
        api.setAuth({ token: accessToken, orgId });
        setOrgId(orgId);
        const next = await loadMe();
        if (!next) {
          logout();
          return { kind: "error", message: "Signed in, but we could not load your account." };
        }
        return { kind: "ok" };
      } catch (err) {
        return { kind: "error", message: (err as Error).message };
      }
    },
    [api, loadMe, logout],
  );

  const selectOrg = React.useCallback(
    (next: string) => {
      api.setAuth({ orgId: next });
      setOrgId(next);
      queryClient.clear();
    },
    [api, queryClient],
  );

  const value: AuthValue = {
    api,
    me,
    orgId,
    ready,
    login,
    verify2fa,
    completeSso,
    selectOrg,
    logout,
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
