import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError, createClient, type ApiClient } from "@/api/client";

export type Membership = {
  org_id: string;
  org_name: string;
  org_slug: string;
  role_name: string;
  /** NOT SENT BY THE SERVER, and never has been. Verified against a live `/auth/me`:
   * MembershipOut's keys are exactly org_id, org_name, org_slug, role_name,
   * identity_verification. The real permission list arrives TOP-LEVEL on `Me`
   * (see `Me.permissions`). This field is kept only so that a server which later starts
   * sending per-membership permissions is honoured without a client change - it must
   * never be treated as the only source, because today it is always undefined. */
  permissions?: string[];
};

/** Permission gate. The list comes from `me.permissions` (top-level on /auth/me);
 * `membership.permissions` is honoured first if a server ever starts sending it, but has
 * never been sent, so it cannot be the only source. When NEITHER is present we fail
 * CLOSED - the same direction api/capabilities.ts:53 already chose for the same absent
 * field. It used to fail OPEN here "for a backend that predates the field", which made
 * the branch unconditional: hasPermission returned true for every permission string for
 * anyone who was a member of the org, so ~18 gated controls (role editing and member 2FA
 * reset on TeamPage, contact merge and GDPR erase, data retention, softphone placing)
 * were shown to people who then got a 403 on click.
 *
 * The server sends an EXPANDED list of explicit strings - an owner gets 34 of them - and
 * there is NO "*" wildcard entry. A plain `includes()` is therefore correct and complete.
 * Do not "improve" this into wildcard/prefix matching without re-checking the server
 * first: matching a "*" that is never sent buys nothing, and a prefix rule would grant
 * permissions the server does not. */
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
  // Split from the two guards around it because it is a different kind of absence. "You are not a
  // member of this workspace" is KNOWN absence of every permission, not absence of
  // information - a stronger denial than the loading case above. It was previously
  // unreachable only because App.tsx drops an orgId that is not in the memberships, i.e.
  // this function's safety depended on a guard in a different file; completeSso is itself
  // an example of a second org-setting path that behaved differently from the first.
  if (!membership) return false;
  // Per-membership first (aspirational - never populated today), then the top-level list,
  // which is where the server actually puts them. No list at all is KNOWN absence of
  // rights, not missing information: fail closed.
  const perms = membership.permissions ?? me.permissions;
  if (!perms) return false;
  return perms.includes(permission);
}

export type Me = {
  /** Effective permissions for the current org membership - the REAL source, sent
   * top-level on /auth/me (Membership.permissions is not sent). An expanded list of
   * explicit strings, no "*" wildcard; optional only because /auth/me is typed loosely
   * here, and undefined is treated as "no permissions" by hasPermission. */
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
  /** Resolves once the post-switch /auth/me (with the NEW X-Org-Id) has landed. Call sites
   * that just want to switch can keep ignoring the promise; tests await it. */
  selectOrg(orgId: string): Promise<void>;
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

  // `me.permissions` is PER-ORG: the /auth/me handler computes it from the caller's role in
  // the org named by the X-Org-Id header. Switching the org without re-asking therefore left
  // `me.permissions` describing the PREVIOUS workspace, and hasPermission() - which now
  // genuinely reads that list instead of returning true for everything - evaluated the old
  // org's rights against the new one until something else happened to reload `me`. An admin
  // in A who is only an agent in B would switch to B and still be shown role editing, member
  // 2FA reset and data retention, each of which 403s on click. completeSso already did the
  // right thing (it calls loadMe); this was the one org-setting path that did not.
  const selectOrg = React.useCallback(
    (next: string): Promise<void> => {
      // ORDERING IS THE FIX. api.setAuth mutates the client's `auth` object SYNCHRONOUSLY
      // (api/client.ts setAuth), and authHeaders reads `api.auth.orgId` at REQUEST time
      // (api/client.ts:86) - it is not React state and does not wait for a re-render. So
      // every request issued after this line, including the /auth/me below, carries
      // `X-Org-Id: next`. Sequencing off `setOrgId` instead would race: React state updates
      // are async, and the refetch would reinstate exactly the stale permissions it exists
      // to replace.
      api.setAuth({ orgId: next });
      setOrgId(next);
      queryClient.clear();
      // Deliberately NOT loadMe(): that callback closes over the PRE-switch `orgId` and uses
      // it for its "is my selected org still one of my memberships?" guard. On a switch that
      // guard would test the org we just LEFT against the fresh memberships and, if the
      // person had been removed from it, null out the org-2 selection that was just made -
      // dumping them back at the org picker on a legitimate switch. So this refreshes `me`
      // and nothing else.
      //
      // The guard is not re-stated against `next` either, deliberately: selectOrg has never
      // validated the org it is given (App.tsx drops an orgId that is not in the memberships,
      // and hasPermission fails closed for a non-membership), and widening this fix to add
      // that would be a second, unrelated behaviour change on the softphone's org-switch
      // teardown path. This is about `me` only.
      //
      // No loading flag on purpose: the whole app must not blank out on every org switch.
      // `me` keeps its previous value for the one round trip, which is the pre-existing
      // behaviour; clearing the query cache above already handles everything else.
      return (async () => {
        try {
          setMe(await api.request<Me>("/api/v1/auth/me"));
        } catch {
          // Same direction as loadMe: an unanswerable /auth/me is "we do not know", and
          // hasPermission fails closed on a null `me`. Keeping the PREVIOUS org's list here
          // would be the original bug, so dropping it is the only safe option.
          setMe(null);
        }
      })();
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
