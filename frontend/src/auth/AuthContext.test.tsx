import { describe, expect, it } from "vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, hasPermission, useAuth, type Me } from "./AuthContext";
import { makeStubClient } from "@/test/harness";

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
