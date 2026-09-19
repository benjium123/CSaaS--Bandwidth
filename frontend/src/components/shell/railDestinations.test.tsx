/**
 * Where the inbox rail's former rows went.
 *
 * The operator's rail is Contacts / Campaigns / Settings and nothing else, so Calls, Setup
 * and Trust & safety - all three full routes - moved into the Settings surface. This file
 * is the other half of InboxColumn.test.tsx's "no longer carries Calls, Setup or Trust &
 * safety": proving they are gone from the rail is only half an answer, and a destination
 * that is gone from both places is a stranded page, not a simplification.
 *
 * It also pins the gating, which is the part a move can quietly break. Calls is gated on
 * `calls:read` and Setup on workspace state, both by way of the SAME `useRailNav` the rails
 * use; Trust & safety is gated on `is_platform_operator`, which is not a capability at all.
 * A member without the permission must still not see the row here.
 *
 * Every case renders a section the caller CANNOT view, so SettingsPage shows its
 * access-denied card instead of mounting a real settings page - the nav is what is under
 * test, and this keeps the file free of a dozen page mocks.
 */
import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, type Me } from "@/auth/AuthContext";
import { makeStubClient } from "@/test/harness";
import { SettingsPage } from "@/pages/SettingsPage";

const SETUP_DONE = {
  has_provider: true,
  has_number: true,
  member_count: 2,
  registration_state: "approved",
};

const SETUP_INCOMPLETE = {
  has_provider: false,
  has_number: false,
  member_count: 1,
  registration_state: "none",
};

const ME: Me = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "agent" }],
};

const OPERATOR_ME: Me = { ...ME, is_platform_operator: true };

function renderSettings(options: { permissions?: string[]; org?: unknown; me?: Me } = {}) {
  const client = makeStubClient({
    "/api/v1/auth/me": options.me ?? ME,
    "/api/v1/me/capabilities": {
      permissions: options.permissions ?? [],
      org: options.org ?? SETUP_DONE,
    },
    "/api/v1/orgs/current": { id: "org-1", name: "Org", slug: "org" },
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        {/* /settings/workspace with no org:read: the section exists, so SettingsPage
            renders its nav, but the panel is the access-denied card. */}
        <MemoryRouter initialEntries={["/settings/workspace"]}>
          <Routes>
            <Route path="/settings/:section" element={<SettingsPage />} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

async function settingsNav() {
  return screen.findByRole("navigation", { name: "Settings" });
}

describe("the destinations that moved out of the inbox rail", () => {
  it("offers Calls to a member who holds calls:read", async () => {
    renderSettings({ permissions: ["calls:read"] });

    const nav = await settingsNav();
    const calls = await within(nav).findByRole("link", { name: "Calls" });
    expect(calls).toHaveAttribute("href", "/calls");
  });

  it("hides Calls from a member who does not - the gate survived the move", async () => {
    renderSettings({ permissions: ["contacts:read"] });

    const nav = await settingsNav();
    // Something rendered: the nav is present and the page reached its denied state, so
    // the absence below is not "nothing has loaded yet".
    expect(await screen.findByText("You do not have access to this setting.")).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Calls" })).not.toBeInTheDocument();
  });

  it("offers Setup while the workspace still has setup to do", async () => {
    renderSettings({ permissions: [], org: SETUP_INCOMPLETE });

    const nav = await settingsNav();
    const setup = await within(nav).findByRole("link", { name: "Setup" });
    expect(setup).toHaveAttribute("href", "/setup");
  });

  it("drops Setup once the workspace is finished, exactly as the rails do", async () => {
    renderSettings({ permissions: [], org: SETUP_DONE });

    const nav = await settingsNav();
    expect(await screen.findByText("You do not have access to this setting.")).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Setup" })).not.toBeInTheDocument();
  });

  it("offers Trust & safety to a platform operator", async () => {
    renderSettings({ permissions: [], me: OPERATOR_ME });

    const nav = await settingsNav();
    const ops = await within(nav).findByRole("link", { name: "Trust & safety" });
    expect(ops).toHaveAttribute("href", "/ops");
  });

  it("hides Trust & safety from everybody else", async () => {
    renderSettings({ permissions: ["calls:read"] });

    const nav = await settingsNav();
    // Calls proves the "More" group rendered at all.
    await within(nav).findByRole("link", { name: "Calls" });
    expect(
      within(nav).queryByRole("link", { name: "Trust & safety" }),
    ).not.toBeInTheDocument();
  });

  it("does not repeat the destinations the inbox rail already lists", async () => {
    renderSettings({ permissions: ["contacts:read", "campaigns:read", "calls:read"] });

    const nav = await settingsNav();
    await within(nav).findByRole("link", { name: "Calls" });
    for (const label of ["Contacts", "Campaigns", "Inbox"]) {
      expect(within(nav).queryByRole("link", { name: label })).not.toBeInTheDocument();
    }
  });
});
