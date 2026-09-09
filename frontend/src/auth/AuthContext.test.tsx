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
  const { logout, selectOrg } = useAuth();
  return (
    <div>
      <button type="button" onClick={logout}>
        Log out
      </button>
      <button type="button" onClick={() => selectOrg("org-2")}>
        Switch org
      </button>
    </div>
  );
}

function renderProbe(client = makeStubClient({ "/api/v1/auth/me": ME })) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
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
  it("fails open when the membership has no permissions array yet (not rolled out)", () => {
    expect(hasPermission(ME, "org-1", "calls:place")).toBe(true);
  });

  it("is authoritative once permissions are present", () => {
    expect(hasPermission(ME, "org-2", "calls:place")).toBe(true);
    expect(hasPermission(ME, "org-2", "numbers:write")).toBe(false);
  });

  it("fails open with no user or no org selected", () => {
    expect(hasPermission(null, "org-1", "calls:place")).toBe(true);
    expect(hasPermission(ME, null, "calls:place")).toBe(true);
  });
});
