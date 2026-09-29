import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { openCommandPalette } from "@/components/ui/CommandPalette";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { TopBar } from "./TopBar";

// Keep the module's other exports real; only the side-effecting opener is replaced.
vi.mock("@/components/ui/CommandPalette", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/ui/CommandPalette")>();
  return { ...actual, openCommandPalette: vi.fn() };
});

const SETUP_DONE = {
  has_provider: true,
  has_number: true,
  member_count: 2,
  registration_state: "approved",
};

const CAPS = {
  permissions: ["contacts:read", "calls:read", "campaigns:read", "settings:read"],
  org: SETUP_DONE,
};

const ME = {
  id: "u1",
  email: "ada@example.com",
  full_name: "Ada Lovelace",
  permissions: ["calls:place", "contacts:read", "calls:read"],
  memberships: [
    { org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner" },
  ],
};

function makeClient(capabilities: unknown = CAPS) {
  return makeStubClient({
    "/api/v1/auth/me": ME,
    "/api/v1/me": ME,
    "/api/v1/me/capabilities": capabilities,
    "/api/v1/me/call-prefs": { dnd: false, dnd_until: null, forward_to: null, teammates: [] },
    "/api/v1/me/notifications": { items: [], unread_count: 0 },
    "/api/v1/support/contacts": {
      email: "help@example.com",
      phone: "+15551234567",
      knowledge_base_url: "https://help.example.com",
      whats_new_url: "https://example.com/new",
      status_url: "https://status.example.com",
      terms_url: "https://example.com/terms",
      privacy_url: "https://example.com/privacy",
    },
  });
}

describe("TopBar", () => {
  beforeEach(() => {
    vi.mocked(openCommandPalette).mockClear();
  });

  it("renders the rail destinations plus Settings from the same capabilities gate", async () => {
    renderWithProviders(<TopBar />, makeClient());

    const nav = await screen.findByRole("navigation", { name: "Main" });
    await within(nav).findByRole("link", { name: "Inbox" });

    expect(
      within(nav)
        .getAllByRole("link")
        .map((link) => link.getAttribute("aria-label")),
    ).toEqual(["Inbox", "Contacts", "Campaigns", "Settings"]);
  });

  it("marks the nav busy and empty while capabilities load", async () => {
    renderWithProviders(<TopBar />, makeClient(new Promise(() => undefined)));

    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(nav).toHaveAttribute("aria-busy", "true");
    expect(within(nav).queryAllByRole("link")).toHaveLength(0);
  });

  it("opens the command palette from the Search button", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TopBar />, makeClient());

    await user.click(await screen.findByRole("button", { name: "Search" }));
    expect(vi.mocked(openCommandPalette)).toHaveBeenCalledTimes(1);
  });

  it("shows the unread badge on Inbox", async () => {
    renderWithProviders(<TopBar inboxUnread={3} />, makeClient());

    const nav = await screen.findByRole("navigation", { name: "Main" });
    const inbox = await within(nav).findByRole("link", { name: "Inbox" });
    const badge = within(inbox).getByText("3");
    expect(badge).toHaveAttribute("aria-label", "3 unread");
  });

  it("shows the theme toggle in the bar and sign out in the account menu", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TopBar />, makeClient());

    // Light / dark is one click, always visible - not buried in the menu.
    const bar = await screen.findByRole("banner", { name: "Top bar" });
    expect(within(bar).getByRole("button", { name: /Switch to the/ })).toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: "Account" }));
    const menu = await screen.findByRole("menu", { name: "Account" });
    expect(within(menu).getByRole("menuitem", { name: "Sign out" })).toBeInTheDocument();
  });

  it("switches workspace from the top bar", async () => {
    const user = userEvent.setup();
    renderWithProviders(<TopBar />, makeClient());
    await user.click(await screen.findByRole("button", { name: "Switch workspace" }));
    expect(await screen.findByRole("menu", { name: "Workspaces" })).toBeInTheDocument();
  });
});
