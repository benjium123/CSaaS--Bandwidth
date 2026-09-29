/**
 * Where the inbox rail's former rows went.
 *
 * The operator's rail is Contacts / Campaigns / Settings and nothing else, so Setup and
 * Trust & safety - both full routes - live inside the Settings surface as well as in the top
 * bar. This file is the other half of InboxColumn.test.tsx's "no longer carries Calls, Setup
 * or Trust & safety": proving they are gone from the rail is only half an answer, and a
 * destination that is gone from both places is a stranded page, not a simplification.
 *
 * Calls used to be in that list and is deliberately NOT any more. The inbox's own Calls tab
 * replaced the page, so /calls is now a redirect into it and NOTHING offers a navigation row
 * for it - not the rail, not the top bar, not Settings. That absence is asserted below.
 *
 * It also pins the gating, which is the part a move can quietly break. Setup is gated on
 * workspace state by way of the SAME `useRailNav` the rails use; Trust & safety is gated on
 * `is_platform_operator`, which is not a capability at all. A member without the permission
 * must still not see the row here.
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

const DENIED = "You do not have access to this setting.";

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

/** The denied card is the signal that the page has settled, so an absence below is real. */
async function settled() {
  return screen.findByText(DENIED);
}

describe("the destinations that moved out of the inbox rail", () => {
  it("no longer offers Calls in Settings - the inbox's Calls tab replaced the page", async () => {
    renderSettings({ permissions: ["calls:read"] });

    const nav = await settingsNav();
    await settled();
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
    await settled();
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
    await settled();
    expect(within(nav).queryByRole("link", { name: "Trust & safety" })).not.toBeInTheDocument();
  });

  it("does not repeat what the inbox rail already lists - or the Calls tab", async () => {
    renderSettings({ permissions: ["contacts:read", "campaigns:read", "calls:read"] });

    const nav = await settingsNav();
    await settled();
    for (const label of ["Contacts", "Campaigns", "Inbox", "Calls"]) {
      expect(within(nav).queryByRole("link", { name: label })).not.toBeInTheDocument();
    }
  });
});
