import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, hasPermission, useAuth, type Me } from "./AuthContext";
import { makeStubClient } from "@/test/harness";
import { OrgPickerPage } from "@/pages/OrgPickerPage";

const ME: Me = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [
    { org_id: "org-1", org_name: "Org 1", org_slug: "org-1", role_name: "admin" },
    {
      org_id: "org-2",
      org_name: "Org 2",
      org_slug: "org-2",
      role_name: "member",
      permissions: ["calls:place"],
    },
  ],
};

/** Exposes logout/selectOrg buttons so a test can exercise the P20 "clear on every
 * identity boundary" contract from the outside. Deliberately has no `useQuery` of its
 * own - a live query observer would immediately refetch the instant its cache entry is
 * cleared (a query with no cache entry always fetches on mount regardless of staleTime),
 * masking whether `queryClient.clear()` actually ran. The test seeds/reads the cache
 * directly via the `queryClient` handle instead. */
function Probe() {
  const { logout, selectOrg, completeSso } = useAuth();
  return (
    <div>
      <button type="button" onClick={logout}>
        Log out
      </button>
      <button type="button" onClick={() => selectOrg("org-2")}>
        Switch org
      </button>
      <button type="button" onClick={() => void completeSso(null, "org-2")}>
        Complete SSO
      </button>
    </div>
  );
}

function renderProbe(client = makeStubClient({ "/api/v1/auth/me": ME })) {
  const queryClient = new QueryClient({
    // gcTime: Infinity, NOT 0, and this is load-bearing. `setQueryData` creates an entry
    // with no observers, so under `gcTime: 0` it is garbage-collected the moment the test
    // yields - and every assertion below then reads `undefined` whether or not
    // `queryClient.clear()` ever ran. Under 0 all four of these tests pass with the
    // clearing REMOVED: they cannot fail, so they prove nothing. Under Infinity the entry
    // survives unless something deliberately clears it, which is the property being tested.
    // This was found when a new test for an genuinely missing clear() passed anyway.
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <Probe />
      </AuthProvider>
    </QueryClientProvider>,
  );
  return { queryClient, client };
}

describe("AuthContext cache hygiene", () => {
  it("clears every cached query on logout", async () => {
    const { queryClient } = renderProbe();
    queryClient.setQueryData(["probe"], "secret-org-1-data");
    expect(queryClient.getQueryData(["probe"])).toBe("secret-org-1-data");

    await userEvent.click(screen.getByRole("button", { name: "Log out" }));

    expect(queryClient.getQueryData(["probe"])).toBeUndefined();
  });

  it("clears every cached query on selectOrg (org switch)", async () => {
    const { queryClient } = renderProbe();
    queryClient.setQueryData(["probe"], "secret-org-1-data");

    await userEvent.click(screen.getByRole("button", { name: "Switch org" }));

    expect(queryClient.getQueryData(["probe"])).toBeUndefined();
  });

  /**
   * completeSso is the FOURTH path that changes the org, and the only one that was never
   * added to this contract. It matters because the console's cross-tenant safety is a
   * chokepoint: most of the 267 query keys do not contain the org, and are kept honest by
   * clearing everything whenever the org changes. An SSO callback is reachable from the
   * authenticated route table (App.tsx), so a signed-in user with a warm cache for
   * workspace A can complete an SSO sign-in into workspace B in the same tab. The sharp
   * one is CAPABILITIES_QUERY_KEY, which useGate() reads to gate the nav rail and every
   * settings section - A's answers, under B's context.
   */
  it("clears every cached query when completeSso lands the user in a workspace", async () => {
    const { queryClient } = renderProbe();
    queryClient.setQueryData(["probe"], "secret-org-1-data");

    await userEvent.click(screen.getByRole("button", { name: "Complete SSO" }));

    expect(queryClient.getQueryData(["probe"])).toBeUndefined();
  });

  it("clears every cached query when a 401 fires onUnauthorized", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": ME });
    const { queryClient } = renderProbe(client);
    queryClient.setQueryData(["probe"], "secret-org-1-data");

    act(() => {
      client.onUnauthorized?.();
    });

    expect(queryClient.getQueryData(["probe"])).toBeUndefined();
  });

  // Item 1: onUnauthorized (the softphone WS's 4401 close path calls this exactly the
  // same way a REST 401 does) must fully log out - including dropping the stored API
  // token/orgId - not just clear in-memory me/orgId/query-cache.
  it("clears the stored API auth token/orgId when onUnauthorized fires, same as logout()", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": ME });
    expect(client.auth.token).toBe("test-token");
    expect(client.auth.orgId).toBe("org-1");

    renderProbe(client);

    act(() => {
      client.onUnauthorized?.();
    });

    expect(client.auth.token).toBeNull();
    expect(client.auth.orgId).toBeNull();
  });
});

describe("hasPermission", () => {
  /** The real /auth/me shape: permissions top-level, memberships WITHOUT the field. */
  function meWith(permissions?: string[]): Me {
    return {
      id: "u1",
      email: "a@example.com",
      full_name: "A",
      ...(permissions ? { permissions } : {}),
      memberships: [{ org_id: "org-1", org_name: "Org 1", org_slug: "org-1", role_name: "owner" }],
    };
  }

  // THE REGRESSION. Verified live against the running backend: MembershipOut has no
  // `permissions` key and never has had one, so the old `if (!membership.permissions)
  // return true` was UNCONDITIONAL - hasPermission answered `true` for every permission
  // string, for anyone who was a member of the org. A user whose top-level list is empty
  // has NO permissions; that is known absence, not absence of information.
  it("fails CLOSED for a member whose top-level permission list is empty", () => {
    const me = meWith([]);
    expect(me.memberships[0].permissions).toBeUndefined();
    expect(hasPermission(me, "org-1", "members:write")).toBe(false);
  });

  // Pairs with the test above so it cannot pass vacuously: the same shape with a
  // non-empty top-level list must still grant the permission it actually contains.
  it("reads the top-level permissions when the membership has none", () => {
    const me = meWith(["members:write"]);
    expect(hasPermission(me, "org-1", "members:write")).toBe(true);
    expect(hasPermission(me, "org-1", "org:update")).toBe(false);
  });

  it("prefers a per-membership permissions array when the server does send one", () => {
    const me = meWith(["members:write"]);
    me.memberships[0].permissions = ["calls:place"];
    expect(hasPermission(me, "org-1", "calls:place")).toBe(true);
    // The top-level list must NOT leak through once the membership is authoritative.
    expect(hasPermission(me, "org-1", "members:write")).toBe(false);
  });

  it("fails CLOSED when neither list is present at all", () => {
    expect(hasPermission(meWith(), "org-1", "calls:place")).toBe(false);
  });

  // Realistic owner payload: an EXPANDED list of explicit strings with no "*" wildcard,
  // so a plain includes() is the right test. Pinned here because "improving" this into
  // wildcard matching, without the server actually sending "*", denies everything.
  it("matches explicit strings from a realistic owner payload (no wildcard)", () => {
    const me = meWith([
      "calls:place",
      "calls:read",
      "calls:supervise",
      "campaigns:manage",
      "roles:write",
      "members:update",
      "settings:read",
      "settings:write",
      "contacts:write",
      "org:read",
    ]);
    expect(me.permissions).not.toContain("*");
    expect(hasPermission(me, "org-1", "roles:write")).toBe(true);
    expect(hasPermission(me, "org-1", "compliance:manage")).toBe(false);
  });

  // The expectation here is INVERTED from what it originally asserted, deliberately. It
  // used to pin `true` for both, i.e. it pinned the defect: a null `me` means /auth/me has
  // not answered, which is "unknown", and answering "permitted" to unknown showed admin
  // affordances on every page load.
  it("fails closed when we do not know yet - no user loaded, or no org selected", () => {
    expect(hasPermission(null, "org-1", "calls:place")).toBe(false);
    expect(hasPermission(ME, null, "calls:place")).toBe(false);
  });

  it("fails closed when the selected org is not one of the memberships", () => {
    expect(hasPermission(meWith(["calls:place"]), "org-nope", "calls:place")).toBe(false);
  });

  it("is authoritative for a membership that does carry permissions", () => {
    expect(hasPermission(ME, "org-2", "calls:place")).toBe(true);
    expect(hasPermission(ME, "org-2", "numbers:write")).toBe(false);
  });
});

/**
 * `me.permissions` is PER-ORG - the /auth/me handler computes it from the caller's role in
 * the org named by the X-Org-Id header. selectOrg used to set the new org and clear the
 * query cache without re-asking, so hasPermission() kept evaluating the PREVIOUS org's
 * permission list against the new org: an admin in A who is an agent in B switched to B and
 * was still shown role editing, member 2FA reset and data retention, each of which 403s.
 */
describe("selectOrg re-fetches /auth/me for the org it switched to", () => {
  const A_ONLY = "roles:write"; // admin in org-1, not granted in org-2
  const B_PERM = "calls:place"; // agent in org-2

  /** Same memberships in both payloads; only the TOP-LEVEL permission list differs, which
   * is exactly how the server behaves. No per-membership `permissions` key - the server has
   * never sent one, and setting it here would make hasPermission read that instead, so the
   * test would stop exercising the top-level list the bug is actually about. */
  const ME_IN_A: Me = {
    id: "u1",
    email: "a@example.com",
    full_name: "A",
    permissions: [A_ONLY, "members:update", "org:read"],
    memberships: [
      { org_id: "org-1", org_name: "Org 1", org_slug: "org-1", role_name: "admin" },
      { org_id: "org-2", org_name: "Org 2", org_slug: "org-2", role_name: "agent" },
    ],
  };
  const ME_IN_B: Me = { ...ME_IN_A, permissions: [B_PERM, "org:read"] };

  type Auth = ReturnType<typeof useAuth>;

  function Probe2({ sink }: { sink: { current: Auth | null } }) {
    const auth = useAuth();
    sink.current = auth;
    return <div data-testid="org">{auth.orgId ?? "none"}</div>;
  }

  /** Sequenced by CALL ORDER, not by the header, so the org-id assertions below stay
   * independent of the payload: a stub that chose its answer from `client.auth.orgId` would
   * hand the right permissions back even to an implementation that sent a stale header. */
  function sequencedClient(payloads: (Me | Error)[]) {
    const seenOrgIds: (string | null)[] = [];
    let i = 0;
    const client = makeStubClient({
      "/api/v1/auth/me": () => {
        // Sampled at REQUEST time - this is the value authHeaders() puts in X-Org-Id.
        seenOrgIds.push(client.auth.orgId);
        return payloads[Math.min(i++, payloads.length - 1)];
      },
    });
    return { client, seenOrgIds };
  }

  async function renderSwitcher(client: ReturnType<typeof makeStubClient>) {
    const sink: { current: Auth | null } = { current: null };
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
    await act(async () => {
      render(
        <QueryClientProvider client={queryClient}>
          <AuthProvider client={client}>
            <Probe2 sink={sink} />
          </AuthProvider>
        </QueryClientProvider>,
      );
    });
    return { sink, queryClient };
  }

  it("replaces the previous org's permissions with the new org's", async () => {
    const { client } = sequencedClient([ME_IN_A, ME_IN_B]);
    const { sink } = await renderSwitcher(client);

    // Precondition: we are in org-1 and hold org-1's admin rights.
    expect(sink.current!.orgId).toBe("org-1");
    expect(hasPermission(sink.current!.me, "org-1", A_ONLY)).toBe(true);

    await act(async () => {
      await sink.current!.selectOrg("org-2");
    });

    expect(sink.current!.orgId).toBe("org-2");
    // The whole point: A's admin permission must NOT survive the switch into B...
    expect(hasPermission(sink.current!.me, "org-2", A_ONLY)).toBe(false);
    // ...and B's own permission must be live, so this cannot pass by merely losing `me`.
    expect(sink.current!.me).not.toBeNull();
    expect(hasPermission(sink.current!.me, "org-2", B_PERM)).toBe(true);
  });

  /** Counting calls would not catch the real bug: a refetch issued with the PREVIOUS
   * X-Org-Id reinstates exactly the stale permission list it was meant to replace. The
   * client's `auth` object is not React state, so it is `api.setAuth({ orgId })` - not
   * `setOrgId` - that has to happen first. This asserts the value the header is built from,
   * sampled inside the stub at the moment the request is made. */
  it("issues that /auth/me with the NEW org id in scope, not the old one", async () => {
    const { client, seenOrgIds } = sequencedClient([ME_IN_A, ME_IN_B]);
    const { sink } = await renderSwitcher(client);
    expect(seenOrgIds).toEqual(["org-1"]);

    await act(async () => {
      await sink.current!.selectOrg("org-2");
    });

    expect(seenOrgIds).toEqual(["org-1", "org-2"]);
    expect(client.calls.filter((c) => c.path === "/api/v1/auth/me")).toHaveLength(2);
  });

  /** The trap that kept this unfixed: `loadMe` closes over the PRE-switch orgId and uses it
   * for its "still a member?" guard. Reusing it naively here would test org-1 against the
   * fresh memberships and, finding it gone, null out the org-2 selection that had just been
   * made - dumping the user back at the org picker on a legitimate switch. */
  it("keeps the new org selected even when the org left behind is gone from memberships", async () => {
    const withoutOrg1: Me = {
      ...ME_IN_B,
      memberships: [{ org_id: "org-2", org_name: "Org 2", org_slug: "org-2", role_name: "agent" }],
    };
    const { client } = sequencedClient([ME_IN_A, withoutOrg1]);
    const { sink } = await renderSwitcher(client);

    await act(async () => {
      await sink.current!.selectOrg("org-2");
    });

    expect(sink.current!.orgId).toBe("org-2");
    expect(client.auth.orgId).toBe("org-2");
    expect(hasPermission(sink.current!.me, "org-2", B_PERM)).toBe(true);
  });

  /** Scope pin. selectOrg has never validated the org it is handed, and this fix did not
   * start: if /auth/me comes back without the org that was selected, the selection STAYS
   * (App.tsx is what drops an unknown orgId) - what must not happen is the gate answering
   * yes off the previous org's list. hasPermission fails closed for a non-membership, so
   * the affordance is denied without selectOrg having to take on org validation. */
  it("leaves org validation alone, but still denies on the new org's own list", async () => {
    const withoutOrg2: Me = {
      ...ME_IN_A,
      memberships: [{ org_id: "org-1", org_name: "Org 1", org_slug: "org-1", role_name: "admin" }],
    };
    const { client } = sequencedClient([ME_IN_A, withoutOrg2]);
    const { sink } = await renderSwitcher(client);

    await act(async () => {
      await sink.current!.selectOrg("org-2");
    });

    expect(sink.current!.orgId).toBe("org-2");
    expect(client.auth.orgId).toBe("org-2");
    expect(hasPermission(sink.current!.me, "org-2", A_ONLY)).toBe(false);
  });

  /** P20 must not regress: the refetch is additive to the cache wipe, not a replacement. */
  it("still clears every cached query on the switch", async () => {
    const { client } = sequencedClient([ME_IN_A, ME_IN_B]);
    const { sink, queryClient } = await renderSwitcher(client);
    queryClient.setQueryData(["probe"], "secret-org-1-data");
    expect(queryClient.getQueryData(["probe"])).toBe("secret-org-1-data");

    await act(async () => {
      await sink.current!.selectOrg("org-2");
    });

    expect(queryClient.getQueryData(["probe"])).toBeUndefined();
  });

  /** A failed refetch must not leave the old org's permissions readable under the new org.
   * hasPermission fails closed on a null `me`, which is the safe direction. */
  it("does not keep the old org's permissions when the refetch fails", async () => {
    const { client } = sequencedClient([ME_IN_A, new Error("boom")]);
    const { sink } = await renderSwitcher(client);
    expect(hasPermission(sink.current!.me, "org-1", A_ONLY)).toBe(true);

    await act(async () => {
      await sink.current!.selectOrg("org-2");
    });

    expect(sink.current!.me).toBeNull();
    expect(hasPermission(sink.current!.me, "org-2", A_ONLY)).toBe(false);
  });
});

/**
 * A list of one is not a choice. Signing up creates a workspace and makes the signer its
 * owner, so the screen immediately after the most important funnel in the product was a
 * picker containing exactly one thing to click. AuthProvider now selects it.
 *
 * `AppLike` reproduces App.tsx's `if (!orgId) return <OrgPickerPage />` branch and nothing
 * else - the assertions are about whether the PICKER is reached, which is that branch's
 * whole content, and rendering the real <App /> would drag in the console shell, the
 * softphone and the router table for a question none of them answer.
 */
describe("single-membership auto-select", () => {
  const base = { id: "u1", email: "a@example.com", full_name: "A" };
  const ORG_1 = { org_id: "org-1", org_name: "Org 1", org_slug: "org-1", role_name: "owner" };
  const ORG_2 = { org_id: "org-2", org_name: "Org 2", org_slug: "org-2", role_name: "member" };

  const SOLO: Me = { ...base, permissions: ["org:read"], memberships: [ORG_1] };
  const TWO: Me = { ...base, permissions: ["org:read"], memberships: [ORG_1, ORG_2] };
  const NONE: Me = { ...base, memberships: [] };
  const OPERATOR_NONE: Me = { ...NONE, is_platform_operator: true };

  const PICKER = "Choose an organization";

  function AppLike() {
    const { me, orgId, ready } = useAuth();
    if (!ready) return <div>Starting</div>;
    if (!me) return <div>Signed out</div>;
    if (!orgId) return <OrgPickerPage />;
    return <div data-testid="console">console:{orgId}</div>;
  }

  /** Samples `client.auth.orgId` at REQUEST time - the value authHeaders() would put in
   * X-Org-Id - so a refetch issued with the wrong org in scope is visible, and so the
   * initial load (null) can be told apart from an auto-select refetch ("org-1"). */
  function clientFor(payload: Me, storedOrgId: string | null = null) {
    const seenOrgIds: (string | null)[] = [];
    const client = makeStubClient({
      "/api/v1/auth/me": () => {
        seenOrgIds.push(client.auth.orgId);
        return payload;
      },
      "/api/v1/auth/logout": {},
    });
    client.setAuth({ orgId: storedOrgId });
    return { client, seenOrgIds };
  }

  async function renderAppLike(client: ReturnType<typeof makeStubClient>) {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: Infinity } },
    });
    await act(async () => {
      render(
        <QueryClientProvider client={queryClient}>
          <AuthProvider client={client}>
            <MemoryRouter>
              <AppLike />
            </MemoryRouter>
          </AuthProvider>
        </QueryClientProvider>,
      );
    });
    return { queryClient };
  }

  const meCalls = (client: ReturnType<typeof makeStubClient>) =>
    client.calls.filter((c) => c.path === "/api/v1/auth/me").length;

  it("selects the only membership instead of showing a picker of one", async () => {
    const { client, seenOrgIds } = clientFor(SOLO);
    await renderAppLike(client);

    expect(screen.queryByText(PICKER)).toBeNull();
    expect(screen.getByTestId("console").textContent).toBe("console:org-1");
    // Not merely React state: `api.auth.orgId` is what authHeaders reads, so this is what
    // every subsequent request is actually scoped to.
    expect(client.auth.orgId).toBe("org-1");
    // Exactly two: the initial load, then selectOrg's refetch - and the refetch carried the
    // NEW org, which is the per-org permission list that path exists to get right.
    expect(seenOrgIds).toEqual([null, "org-1"]);
  });

  /** The pair that stops the test above passing vacuously: the picker is CORRECT here and
   * must still appear, with nothing chosen on the user's behalf. */
  it("leaves the picker alone when there is more than one membership", async () => {
    const { client, seenOrgIds } = clientFor(TWO);
    await renderAppLike(client);

    expect(screen.getByText(PICKER)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Org 1/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Org 2/ })).toBeTruthy();
    expect(screen.queryByTestId("console")).toBeNull();
    expect(client.auth.orgId).toBeNull();
    expect(seenOrgIds).toEqual([null]);
  });

  /** ...and the picker must still WORK when reached deliberately - a two-org user switching. */
  it("still lets a two-org user pick, and that pick is a real org switch", async () => {
    const { client, seenOrgIds } = clientFor(TWO);
    await renderAppLike(client);

    await act(async () => {
      await userEvent.click(screen.getByRole("button", { name: /Org 2/ }));
    });

    expect(screen.getByTestId("console").textContent).toBe("console:org-2");
    expect(client.auth.orgId).toBe("org-2");
    expect(seenOrgIds).toEqual([null, "org-2"]);
  });

  /** Zero memberships is NOT one membership: nothing is auto-selected, the picker keeps
   * saying so, and - the trap - nothing spins. */
  it("does not crash or loop for an account with no memberships", async () => {
    const { client, seenOrgIds } = clientFor(NONE);
    await renderAppLike(client);

    expect(screen.getByText("You are not a member of any organization yet.")).toBeTruthy();
    expect(client.auth.orgId).toBeNull();
    expect(seenOrgIds).toEqual([null]);

    // A loop would show up as further /auth/me calls once the queues drain.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(meCalls(client)).toBe(1);
  });

  /** P43: a platform operator deliberately belongs to no workspace, and App.tsx lets them
   * reach /ops without one. Auto-select must not change that - they still land on the
   * picker surface, which is what offers the operator console. */
  it("keeps the membership-less platform operator on the picker/ops path", async () => {
    const { client, seenOrgIds } = clientFor(OPERATOR_NONE);
    await renderAppLike(client);

    expect(screen.getByText(PICKER)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open the operator console" })).toBeTruthy();
    expect(client.auth.orgId).toBeNull();
    expect(seenOrgIds).toEqual([null]);
  });

  /** A returning single-org user already has the org in storage. The effect must not fire
   * for them: that would be a second /auth/me and a cache clear on every page load. */
  it("does not re-fire when an org is already selected", async () => {
    const { client, seenOrgIds } = clientFor(SOLO, "org-1");
    await renderAppLike(client);

    expect(screen.getByTestId("console").textContent).toBe("console:org-1");
    // One call only: the initial load, already scoped to the stored org. No auto-select.
    expect(seenOrgIds).toEqual(["org-1"]);

    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });
    expect(meCalls(client)).toBe(1);
  });

});
