/**
 * P20a verification probes (Opus verifier, 2026-09-10).
 *
 * These are CHARACTERISATION tests: every assertion below pins the behaviour the code
 * has TODAY, including two defects the verifier reported to Fable. They are written
 * this way on purpose - the verifier is not allowed to change app code, and leaving a
 * red suite behind would hide every other regression. When a defect is fixed, the test
 * that pins it goes red and must be INVERTED (the "correct" assertion is written out in
 * each comment), which is the point.
 *
 * DEFECT N1 - the Settings gear shows for an agent, and /settings then lands the agent
 *   on an access-denied card.
 *   The agent system role holds `compliance:read` (backend app/models/rbac.py:81), and
 *   settingsSections.ts:24 gates the Messaging section on `compliance:read`, so
 *   Sidebar.tsx:44 (`SETTINGS_SECTIONS.some(...)`) renders the gear. The addendum says
 *   the gear appears "only with any settings/admin permission". Worse, App.tsx:88 sends
 *   /settings to /settings/workspace unconditionally and Workspace needs `org:read`,
 *   which an agent does not hold - so the gear leads to "You do not have access to this
 *   setting." CORRECT: no gear for an agent, or /settings redirects to the first
 *   section the caller can actually view.
 *
 * DEFECT N2 - the capabilities fallback fails fully open for every role.
 *   capabilities.ts:55 falls back to `hasPermission(me, orgId, p)`, which reads
 *   `membership.permissions` (AuthContext.tsx:23). The backend's MembershipOut has NO
 *   permissions field (backend app/api/routes/auth.py:58-62) - permissions are a
 *   TOP-LEVEL field on MeOut - so that lookup is always undefined and hasPermission
 *   always returns true. Any error on /me/capabilities therefore shows an agent the
 *   full rail, the Settings gear and all ten settings sections.
 *   CORRECT: fall back to `me.permissions`, or fail closed.
 */
import * as React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, type Me } from "@/auth/AuthContext";
import { makeStubClient } from "@/test/harness";
import { App } from "@/App";
import { Sidebar } from "./Sidebar";

vi.mock("@/softphone/SoftphoneProvider", () => ({
  SoftphoneProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/softphone/SoftphonePanel", () => ({ SoftphonePanel: () => null }));
vi.mock("@/pages/ConversationsPage", () => ({
  ConversationsPage: () => <div>Inbox page</div>,
}));
vi.mock("@/pages/ContactsPage", () => ({ ContactsPage: () => <div>Contacts page</div> }));
vi.mock("@/pages/CallsPage", () => ({ CallsPage: () => <div>Calls page</div> }));
vi.mock("@/pages/CampaignsPage", () => ({ CampaignsPage: () => <div>Campaigns page</div> }));
vi.mock("@/pages/ListsPage", () => ({ ListsPage: () => <div>Lists page</div> }));
vi.mock("@/pages/LoginPage", () => ({ LoginPage: () => <div>Login page</div> }));
vi.mock("@/pages/OrgPickerPage", () => ({ OrgPickerPage: () => <div>Org picker</div> }));
vi.mock("@/pages/AcceptInvitePage", () => ({
  AcceptInvitePage: () => <div>Accept invite</div>,
}));

/** Verbatim from backend app/models/rbac.py SYSTEM_ROLES["agent"]. */
const AGENT_PERMISSIONS = [
  "inbox:read",
  "inbox:send",
  "inbox:manage",
  "contacts:read",
  "contacts:write",
  "compliance:read",
  "templates:read",
  "calls:read",
];

const ORG_SUMMARY = {
  has_provider: true,
  has_number: true,
  member_count: 2,
  registration_state: "approved",
};

/** The REAL shape /api/v1/auth/me returns: permissions at the top level, memberships
 *  without a permissions field. */
const AGENT_ME: Me & { permissions: string[] } = {
  id: "u1",
  email: "agent@example.com",
  full_name: "Agent Person",
  permissions: AGENT_PERMISSIONS,
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "agent" }],
};

function renderWith(ui: React.ReactNode, capabilities: unknown, entries = ["/inbox"]) {
  const client = makeStubClient({
    "/api/v1/auth/me": AGENT_ME,
    "/api/v1/inboxes": [],
    "/api/v1/orgs/current": { id: "org-1", name: "Org", slug: "org" },
    "/api/v1/provider-accounts": [],
    "/api/v1/me/capabilities": capabilities,
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={entries}>{ui}</MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

const AGENT_CAPS = { permissions: AGENT_PERMISSIONS, org: ORG_SUMMARY };

describe("nav gating for a real agent (backend permission set)", () => {
  it("hides Campaigns from an agent - campaigns:read is not in the agent role", async () => {
    renderWith(<Sidebar />, AGENT_CAPS);

    await screen.findByRole("link", { name: "Inbox" });
    const nav = screen.getByRole("navigation", { name: "Sidebar" });
    expect(within(nav).queryByRole("link", { name: "Campaigns" })).not.toBeInTheDocument();
    // Inbox / Contacts / Calls remain.
    expect(within(nav).getAllByRole("link")).toHaveLength(3);
  });

  it("B1 fixed: no Settings gear for an agent (Messaging now needs compliance:manage)", async () => {
    renderWith(<Sidebar />, AGENT_CAPS);

    await screen.findByRole("link", { name: "Inbox" });
    expect(screen.queryByRole("link", { name: "Settings" })).not.toBeInTheDocument();
  });

  it("B1 fixed: /settings sends an agent with no settings sections back to the inbox", async () => {
    renderWith(<App />, AGENT_CAPS, ["/settings"]);

    expect(await screen.findByText("Inbox page")).toBeInTheDocument();
    expect(screen.queryByText("You do not have access to this setting.")).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Settings" })).not.toBeInTheDocument();
  });
});

describe("capabilities fallback when /me/capabilities fails", () => {
  it("B2 fixed: when capabilities errors the rail falls back to me.permissions (3 links, no gear)", async () => {
    renderWith(<Sidebar />, new Error("capabilities unavailable"));

    await screen.findByRole("link", { name: "Inbox" });
    const nav = screen.getByRole("navigation", { name: "Sidebar" });
    expect(within(nav).getAllByRole("link")).toHaveLength(3);
    expect(within(nav).queryByRole("link", { name: "Campaigns" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Settings" })).not.toBeInTheDocument();
  });

  it("renders nothing at all while capabilities is still loading (no flash)", async () => {
    renderWith(<Sidebar />, new Promise<never>(() => undefined));

    const nav = screen.getByRole("navigation", { name: "Sidebar" });
    expect(nav).toHaveAttribute("aria-busy", "true");
    expect(within(nav).queryAllByRole("link")).toHaveLength(0);
    expect(screen.queryByRole("link", { name: "Settings" })).not.toBeInTheDocument();
  });

  it("the 404 fallback path is reached at all (it is not dead code)", async () => {
    // Guards against a future 'retry' or error-swallow change silently turning the
    // fallback off; if this ever renders zero links the fallback stopped running.
    renderWith(<Sidebar />, new Error("404"));
    await screen.findByRole("link", { name: "Inbox" });
    expect(
      within(screen.getByRole("navigation", { name: "Sidebar" })).getAllByRole("link").length,
    ).toBeGreaterThan(0);
  });
});

describe("settings section routing", () => {
  it("every legacy redirect in the addendum resolves to a real section id", async () => {
    // App.tsx redirect targets, checked against settingsSections.ts ids so a rename of
    // a section can never leave a redirect pointing at a 'section not found' bounce.
    const { SETTINGS_SECTIONS } = await import("@/pages/settingsSections");
    const ids = new Set(SETTINGS_SECTIONS.map((s) => s.id));
    for (const target of [
      "billing",
      "ai",
      "calling",
      "numbers",
      "providers",
      "team",
      "developers",
      "workspace",
      "inboxes",
      "messaging",
    ]) {
      expect(ids.has(target as never)).toBe(true);
    }
    expect(SETTINGS_SECTIONS).toHaveLength(10);
  });

  it("an unknown /settings/:section bounces to workspace exactly once", async () => {
    renderWith(<App />, { permissions: ["org:read"], org: ORG_SUMMARY }, [
      "/settings/not-a-section",
    ]);
    // Workspace is what the bounce lands on, and it renders (no redirect loop): the
    // section nav link is marked current and the Workspace panel itself is mounted.
    const link = await screen.findByRole("link", { name: "Workspace" });
    expect(link).toHaveAttribute("href", "/settings/workspace");
    expect(link).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("heading", { name: "Workspace" })).toBeInTheDocument();
  });
});
