import { describe, expect, it } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, type Me } from "@/auth/AuthContext";
import { makeStubClient } from "@/test/harness";
import { MobileTabBar, Sidebar } from "./Sidebar";

/** A workspace that has finished setting up: the rail's Setup entry is absent here, so the
 *  link-count assertions below are about the permanent destinations only. */
const SETUP_DONE = {
  has_provider: true,
  has_number: true,
  member_count: 2,
  registration_state: "approved",
};

/** A brand-new workspace: nothing connected, nobody invited, not registered. */
const SETUP_INCOMPLETE = {
  has_provider: false,
  has_number: false,
  member_count: 1,
  registration_state: "none",
};

const FULL_CAPS = {
  permissions: ["contacts:read", "calls:read", "campaigns:read", "settings:read"],
  org: SETUP_DONE,
};

const ME: Me = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner" }],
};

const ME_TWO: Me = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [
    { org_id: "org-1", org_name: "First org", org_slug: "first", role_name: "owner" },
    { org_id: "org-2", org_name: "Second org", org_slug: "second", role_name: "owner" },
  ],
};

function renderRail(options: { capabilities?: unknown; me?: Me } = {}) {
  const client = makeStubClient({
    "/api/v1/auth/me": options.me ?? ME,
    "/api/v1/me/capabilities": options.capabilities ?? FULL_CAPS,
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter>
          <Sidebar />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
  return client;
}

function renderMobile(capabilities: unknown = FULL_CAPS) {
  const client = makeStubClient({
    "/api/v1/auth/me": ME,
    "/api/v1/me/capabilities": capabilities,
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter>
          <MobileTabBar />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("Sidebar rail", () => {
  it("shows only the four work links and nothing from the old sidebar", async () => {
    renderRail();

    expect(await screen.findByRole("link", { name: "Inbox" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Sidebar" });
    expect(within(nav).getAllByRole("link")).toHaveLength(4);

    for (const label of ["Analytics", "Lists", "More", "Legacy inbox", "Providers", "Numbers", "Team"]) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
  });

  it("hides Campaigns when campaigns:read is missing but keeps Inbox, Contacts and Calls", async () => {
    renderRail({
      capabilities: {
        permissions: ["contacts:read", "calls:read", "settings:read"],
        org: FULL_CAPS.org,
      },
    });

    expect(await screen.findByRole("link", { name: "Inbox" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Sidebar" });
    expect(within(nav).getAllByRole("link")).toHaveLength(3);

    expect(within(nav).getByRole("link", { name: "Inbox" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Contacts" })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: "Calls" })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: "Campaigns" })).not.toBeInTheDocument();
  });

  it("hides the Settings gear without any settings permission and shows it with settings:read", async () => {
    renderRail({
      capabilities: {
        permissions: ["contacts:read", "calls:read", "campaigns:read"],
        org: FULL_CAPS.org,
      },
    });
    await screen.findByRole("link", { name: "Inbox" });
    expect(screen.queryByRole("link", { name: "Settings" })).not.toBeInTheDocument();

    screen.getByRole("navigation", { name: "Sidebar" });
    screen.getByRole("button", { name: "Sign out" });
  });

  it("shows the Settings gear when settings:read is present", async () => {
    renderRail({
      capabilities: {
        permissions: ["contacts:read", "calls:read", "campaigns:read", "settings:read"],
        org: FULL_CAPS.org,
      },
    });

    await screen.findByRole("link", { name: "Inbox" });
    expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument();
  });

  it("renders no nav links while capabilities are still loading", async () => {
    renderRail({ capabilities: new Promise<never>(() => undefined) });

    const nav = screen.getByRole("navigation", { name: "Sidebar" });
    expect(nav).toHaveAttribute("aria-busy", "true");
    expect(within(nav).queryAllByRole("link")).toHaveLength(0);
  });

  it("fails CLOSED when capabilities is rejected and me.permissions is absent (B2)", async () => {
    renderRail({ capabilities: new Error("older backend") });

    expect(await screen.findByRole("link", { name: "Inbox" })).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Sidebar" });
    expect(within(nav).getAllByRole("link")).toHaveLength(1);
    expect(screen.queryByRole("link", { name: "Settings" })).not.toBeInTheDocument();
  });

  it("opens the workspace switcher and selects another membership", async () => {
    const client = renderRail({ me: ME_TWO });

    const trigger = await screen.findByRole("button", { name: "Switch workspace" });
    expect(trigger).toHaveTextContent("F");

    await userEvent.click(trigger);

    const menu = screen.getByRole("menu", { name: "Workspaces" });
    expect(within(menu).getByText("First org")).toBeInTheDocument();
    expect(within(menu).getByText("Second org")).toBeInTheDocument();

    await userEvent.click(within(menu).getByRole("menuitem", { name: "Second org" }));

    await waitFor(() => {
      expect(screen.queryByRole("menu", { name: "Workspaces" })).not.toBeInTheDocument();
      expect(client.auth.orgId).toBe("org-2");
      expect(screen.getByRole("button", { name: "Switch workspace" })).toHaveTextContent("S");
    });
  });
});

describe("MobileTabBar", () => {
  it("renders Inbox, Contacts, Calls and Settings under the bottom navigation label", async () => {
    renderMobile();

    const nav = await screen.findByRole("navigation", { name: "Bottom navigation" });
    const links = within(nav).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("aria-label"))).toEqual([
      "Inbox",
      "Contacts",
      "Calls",
      "Settings",
    ]);
  });
});

/**
 * The setup checklist moved off /inbox and onto its own page. This entry is the ONLY way a
 * new workspace finds it, so these pin both halves: present while there is work to do,
 * gone (not a dead link to an empty page) once there is not.
 */
describe("Setup rail entry", () => {
  it("appears while the workspace is not finished setting up", async () => {
    renderRail({ capabilities: { ...FULL_CAPS, org: SETUP_INCOMPLETE } });

    const setup = await screen.findByRole("link", { name: "Setup" });
    expect(setup).toHaveAttribute("href", "/setup");

    const nav = screen.getByRole("navigation", { name: "Sidebar" });
    expect(within(nav).getAllByRole("link")).toHaveLength(5);
    // It sits with the primary destinations, after them, and does not displace Inbox.
    expect(
      within(nav).getAllByRole("link").map((l) => l.getAttribute("aria-label")),
    ).toEqual(["Inbox", "Contacts", "Calls", "Campaigns", "Setup"]);
  });

  it("is absent once every setup step is done", async () => {
    renderRail({ capabilities: { ...FULL_CAPS, org: SETUP_DONE } });

    await screen.findByRole("link", { name: "Inbox" });
    expect(screen.queryByRole("link", { name: "Setup" })).not.toBeInTheDocument();
  });

  it("appears for a member with no permissions at all - the checklist has never been gated", async () => {
    renderRail({ capabilities: { permissions: [], org: SETUP_INCOMPLETE } });

    expect(await screen.findByRole("link", { name: "Setup" })).toBeInTheDocument();
  });

  it("is absent while capabilities are still loading, and when they failed", async () => {
    renderRail({ capabilities: new Promise<never>(() => undefined) });
    expect(screen.queryByRole("link", { name: "Setup" })).not.toBeInTheDocument();

    // A failed capabilities call leaves gate.org null: we do not know whether setup is
    // outstanding, and a Setup link to a page that cannot say anything is worse than none.
    renderRail({ capabilities: new Error("capabilities unavailable") });
    await screen.findAllByRole("link", { name: "Inbox" });
    expect(screen.queryByRole("link", { name: "Setup" })).not.toBeInTheDocument();
  });

  it("reaches the mobile bar too, between Calls and Settings", async () => {
    renderMobile({ ...FULL_CAPS, org: SETUP_INCOMPLETE });

    const nav = await screen.findByRole("navigation", { name: "Bottom navigation" });
    await within(nav).findByRole("link", { name: "Setup" });
    expect(
      within(nav).getAllByRole("link").map((l) => l.getAttribute("aria-label")),
    ).toEqual(["Inbox", "Contacts", "Calls", "Setup", "Settings"]);
  });
});

/**
 * INBOX_RAIL_PATHS is the split between what the merged 240px inbox rail lists and what is
 * reached through Settings instead. Nothing enforces it at the type level - it is a list of
 * strings - so these pin the two properties that make the split safe.
 */
describe("INBOX_RAIL_PATHS: the split between the inbox rail and Settings", () => {
  it("names only paths that are real rail destinations", async () => {
    const { INBOX_RAIL_PATHS, RAIL_ITEMS } = await import("./Sidebar");
    const known = new Set(RAIL_ITEMS.map((i) => i.to));
    for (const path of INBOX_RAIL_PATHS) {
      expect(known.has(path)).toBe(true);
    }
  });

  it("is exactly the operator's two Workspace destinations, and never /inbox", async () => {
    const { INBOX_RAIL_PATHS } = await import("./Sidebar");
    // Settings is not here because it is gated on "any visible settings section" rather
    // than a permission, and is rendered separately by both rails (SETTINGS_ITEM).
    expect([...INBOX_RAIL_PATHS]).toEqual(["/contacts", "/campaigns"]);
    // The rail IS the inbox; Settings must never offer a second way back to it.
    expect(INBOX_RAIL_PATHS).not.toContain("/inbox");
  });

  it("the icon rail is unaffected by the split - it still lists everything", async () => {
    renderRail({ capabilities: { ...FULL_CAPS, org: SETUP_INCOMPLETE } });

    const nav = await screen.findByRole("navigation", { name: "Sidebar" });
    expect(
      within(nav).getAllByRole("link").map((l) => l.getAttribute("aria-label")),
    ).toEqual(["Inbox", "Contacts", "Calls", "Campaigns", "Setup"]);
  });
});
