import * as React from "react";
import { createClient, type ApiClient } from "@/api/client";

export type Membership = {
  org_id: string;
  org_name: string;
  org_slug: string;
  role_name: string;
};

export type Me = {
  id: string;
  email: string;
  full_name: string;
  memberships: Membership[];
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
  const [me, setMe] = React.useState<Me | null>(null);
  const [orgId, setOrgId] = React.useState<string | null>(api.auth.orgId);
  const [ready, setReady] = React.useState(false);

  const logout = React.useCallback(() => {
    api.setAuth({ token: null, orgId: null });
    setMe(null);
    setOrgId(null);
  }, [api]);

  React.useEffect(() => {
    api.onUnauthorized = () => {
      setMe(null);
      setOrgId(null);
    };
  }, [api]);

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

  const selectOrg = React.useCallback(
    (next: string) => {
      api.setAuth({ orgId: next });
      setOrgId(next);
    },
    [api],
  );

  const value: AuthValue = { api, me, orgId, ready, login, verify2fa, selectOrg, logout };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
