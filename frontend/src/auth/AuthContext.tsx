import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, createClient, type ApiClient } from "@/api/client";

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
  // "We have not asked yet" is NOT "allowed". `me` is null until /auth/me answers, so
  // returning true here made all eleven call sites render admin affordances - role editing
  // and member reset on TeamPage among them - for the duration of every page load. That is
  // the conflation of "false" with "not loaded" that components/auth/AuthShell.tsx's header
  // describes, in the permissive direction, inside the one function whose job is to answer a
  // security question. api/capabilities.ts:52 already states the principle for its own
  // fallback: never show an admin item to an agent because a lookup came back undefined.
  //
  // The visible effect of denying instead is that a control appears a beat late rather than
  // appearing and vanishing - the safe direction, and self-correcting.
  //
  // NOT a privilege escalation either way: the backend enforces all of these with
  // require_permission, so the affordance was always a lie rather than a door.
  if (!me || !orgId) return false;
  const membership = me.memberships.find((m) => m.org_id === orgId);
  // Split from the fail-open below, because only one of these deserves it. "You are not a
  // member of this workspace" is KNOWN absence of every permission, not absence of
  // information - a stronger denial than the loading case above. It was previously
  // unreachable only because App.tsx drops an orgId that is not in the memberships, i.e.
  // this function's safety depended on a guard in a different file; completeSso is itself
  // an example of a second org-setting path that behaved differently from the first.
  if (!membership) return false;
  // THIS fail-open is deliberate and stays: an undefined `permissions` means a backend that
  // predates the field (P20 feature detection), not a member with no rights.
  if (!membership.permissions) return true;
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
  /** P41 */
  has_passkey?: boolean;
  is_platform_operator?: boolean;
  /** P43: "reviewer" | "admin" for platform operators. */
  operator_role?: string | null;
  /** P41: must add an authenticator app or passkey before anything else works. */
  second_factor_required?: boolean;
  /** P42: owner/admin/billing/operator - passkey sign-in required after the grace date. */
  passkey_required?: boolean;
  passkey_grace_until?: string | null;
};

type LoginResult =
  | { kind: "ok" }
  | { kind: "needs_2fa"; pendingToken: string; methods: string[] }
  /**
   * P42: `code` is the API's own `error.code`, carried through so a caller can offer the
   * right NEXT STEP - the one live use is `sso_required`, where the workspace enforces
   * single sign-on and a password will never work, so a plain error message leaves the
   * person with nowhere to go. It exists for AFFORDANCE ONLY: it must never choose the
   * failure wording. A sign-in that failed has to read identically whatever the cause, or
   * the screen becomes an oracle for which addresses have accounts. Passing it on leaks
   * nothing - the browser already received `{"error":{"code":...}}` in the response body;
   * dropping it here was lossy, not protective.
   */
  | { kind: "error"; message: string; code?: string };

/** The error arm, with the API's code kept when the failure came from the API. */
function loginError(err: unknown): LoginResult {
  return {
    kind: "error",
    message: (err as Error).message,
    code: err instanceof ApiError ? err.code : undefined,
  };
}

type AuthValue = {
  api: ApiClient;
  me: Me | null;
  orgId: string | null;
  ready: boolean;
  login(email: string, password: string): Promise<LoginResult>;
  verify2fa(pendingToken: string, code: string): Promise<LoginResult>;
  verifyPasskey(pendingToken: string): Promise<LoginResult>;
  recoverWithCode(pendingToken: string, code: string): Promise<LoginResult>;
  refreshMe(): Promise<Me | null>;
  completeSso(accessToken: string | null, orgId: string): Promise<LoginResult>;
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
    // P42: end the server session (clears the HttpOnly cookie); never block the UI on it.
    void api.request("/api/v1/auth/logout", { method: "POST" }).catch(() => undefined);
    api.setAuth({ token: null, orgId: null });
    setMe(null);
    setOrgId(null);
    queryClient.clear();
  }, [api, queryClient]);

  // Item 1 / P42: onUnauthorized (a REST 401 or a WS 4401 close) means the server already
  // ended the session, so there is nothing to tell it - forget everything locally, exactly
  // like logout() minus the server call (the next person on this browser starts clean).
  const forget = React.useCallback(() => {
    api.setAuth({ token: null, orgId: null });
    setMe(null);
    setOrgId(null);
    queryClient.clear();
  }, [api, queryClient]);

  React.useEffect(() => {
    api.onUnauthorized = forget;
  }, [api, forget]);

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
      // P42: the session cookie is invisible to scripts, so always ask the server.
      await loadMe();
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
          methods?: string[];
        }>("/api/v1/auth/login", { method: "POST", json: { email, password } });

        if (res.requires_2fa && res.pending_token) {
          return {
            kind: "needs_2fa",
            pendingToken: res.pending_token,
            methods: res.methods ?? ["totp"],
          };
        }
        if (res.access_token) api.setAuth({ token: res.access_token });
        await loadMe();
        return { kind: "ok" };
      } catch (err) {
        return loginError(err);
      }
    },
    [api, loadMe],
  );

  const verify2fa = React.useCallback(
    async (pendingToken: string, code: string): Promise<LoginResult> => {
      try {
        const res = await api.request<{ access_token: string | null }>("/api/v1/auth/2fa/verify", {
          method: "POST",
          json: { pending_token: pendingToken, code },
        });
        if (res.access_token) api.setAuth({ token: res.access_token });
        await loadMe();
        return { kind: "ok" };
      } catch (err) {
        return loginError(err);
      }
    },
    [api, loadMe],
  );

  const verifyPasskey = React.useCallback(
    async (pendingToken: string): Promise<LoginResult> => {
      try {
        const { getPasskeyAssertion } = await import("@/lib/webauthn");
        const opts = await api.request<{ challenge_id: string; options: unknown }>(
          "/api/v1/auth/passkeys/login/options",
          { method: "POST", json: { pending_token: pendingToken } },
        );
        const credential = await getPasskeyAssertion(opts.options);
        const res = await api.request<{ access_token: string | null }>(
          "/api/v1/auth/passkeys/login/verify",
          {
            method: "POST",
            json: { pending_token: pendingToken, challenge_id: opts.challenge_id, credential },
          },
        );
        if (res.access_token) api.setAuth({ token: res.access_token });
        await loadMe();
        return { kind: "ok" };
      } catch (err) {
        return loginError(err);
      }
    },
    [api, loadMe],
  );

  const recoverWithCode = React.useCallback(
    async (pendingToken: string, code: string): Promise<LoginResult> => {
      try {
        const res = await api.request<{ access_token: string | null }>(
          "/api/v1/auth/2fa/recovery",
          { method: "POST", json: { pending_token: pendingToken, code } },
        );
        if (res.access_token) api.setAuth({ token: res.access_token });
        await loadMe();
        return { kind: "ok" };
      } catch (err) {
        return loginError(err);
      }
    },
    [api, loadMe],
  );

  const completeSso = React.useCallback(
    async (accessToken: string | null, orgId: string): Promise<LoginResult> => {
      try {
        // WHY: the SSO callback returns the token as JSON rather than redirecting, so
        // the console completes the login itself; setting the org here as well means
        // an SSO user lands straight in their workspace instead of the org picker.
        api.setAuth({ token: accessToken, orgId });
        // P42: the SSO callback also set the session cookie; accessToken is null then.
        setOrgId(orgId);
        // This is the FOURTH path that changes the org, and it was the only one that did
        // not clear the cache. That matters because cross-tenant safety here is a
        // chokepoint rather than a property of the keys: most query keys do not contain the
        // org, and are kept honest by wiping everything whenever the org changes. An SSO
        // callback is in the AUTHENTICATED route table too, so someone sitting in workspace
        // A with a warm cache can complete an SSO sign-in into workspace B in the same tab.
        // The sharp entry is CAPABILITIES_QUERY_KEY: useGate() reads it to gate the nav rail
        // and every settings section, so for a beat the console would answer permission
        // questions with A's answers while the user is in B.
        queryClient.clear();
        const next = await loadMe();
        if (!next) {
          logout();
          return { kind: "error", message: "Signed in, but we could not load your account." };
        }
        return { kind: "ok" };
      } catch (err) {
        return loginError(err);
      }
    },
    [api, loadMe, logout, queryClient],
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
    verifyPasskey,
    refreshMe: loadMe,
    recoverWithCode,
    completeSso,
    selectOrg,
    logout,
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
