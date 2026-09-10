import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, type Me } from "@/auth/AuthContext";
import { makeStubClient } from "@/test/harness";
import { SettingsPage } from "./SettingsPage";

vi.mock("@/pages/TeamPage", () => ({
  TeamPage: () => <div>Team page</div>,
}));
vi.mock("@/pages/SettingsSecurityPage", () => ({
  SettingsSecurityPage: () => <div>Security page</div>,
}));
vi.mock("@/pages/InboxSettingsPage", () => ({
  InboxSettingsPage: () => <div>Inboxes settings page</div>,
}));
vi.mock("@/pages/NumbersPage", () => ({
  NumbersPage: () => <div>Numbers page</div>,
}));
vi.mock("@/pages/ProvidersPage", () => ({
  ProvidersPage: () => <div>Providers page</div>,
}));
vi.mock("@/pages/FlowsPage", () => ({
  FlowsPage: () => <div>Flows page</div>,
}));
vi.mock("@/pages/QueuesPage", () => ({
  QueuesPage: () => <div>Queues page</div>,
}));
vi.mock("@/pages/AgentPage", () => ({
  AgentPage: () => <div>Agent page</div>,
}));
vi.mock("@/pages/AppointmentsPage", () => ({
  AppointmentsPage: () => <div>Appointments page</div>,
}));
vi.mock("@/components/assistants/AiProvidersTab", () => ({
  AiProvidersTab: () => <button>Add a connection</button>,
}));
vi.mock("@/components/assistants/KnowledgeTab", () => ({
  KnowledgeTab: () => <div>Knowledge page</div>,
}));
vi.mock("@/pages/PlatformPage", () => ({
  PlatformPage: () => <div>Platform page</div>,
}));
vi.mock("@/pages/DashboardPage", () => ({
  DashboardPage: () => <div>Dashboard page</div>,
}));
vi.mock("@/components/spend/SpendCard", () => ({
  SpendCard: ({ provider }: { provider: string }) => <div>Spend card {provider}</div>,
}));

const ME: Me = {
  id: "u1",
  email: "owner@example.com",
  full_name: "Owner Person",
  memberships: [
    {
      org_id: "org-1",
      org_name: "Org",
      org_slug: "org",
      role_name: "owner",
      permissions: ["settings:read", "settings:write"],
    },
  ],
};

const ORG = { id: "org-1", name: "Org Name", slug: "org" };

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}{location.search}</div>;
}

function renderSettings({
  initialEntries = ["/settings/team"],
  permissions = ["org:read", "members:read", "inboxes:admin", "numbers:read", "settings:read", "compliance:read", "settings:write"],
  org = ORG,
  providerAccounts = [],
  memberCount = 1,
}: {
  initialEntries?: string[];
  permissions?: string[];
  org?: { id: string; name: string; slug: string };
  providerAccounts?: unknown[];
  memberCount?: number;
} = {}) {
  const client = makeStubClient({
    "/api/v1/auth/me": ME,
    "/api/v1/me/capabilities": {
      permissions,
      org: { has_provider: false, has_number: false, member_count: memberCount, registration_state: "none" },
    },
    "/api/v1/orgs/current": org,
    "/api/v1/provider-accounts": providerAccounts,
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });

  return {
    client,
    ...render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider client={client}>
          <MemoryRouter initialEntries={initialEntries}>
            <LocationProbe />
            <Routes>
              <Route path="/settings/:section" element={<SettingsPage />} />
              <Route path="/settings" element={<SettingsPage />} />
            </Routes>
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    ),
  };
}

describe("SettingsPage", () => {
  it("lists only the sections permitted by capabilities", async () => {
    renderSettings({
      initialEntries: ["/settings/workspace"],
      permissions: [
        "org:read",
        "members:read",
        "inboxes:admin",
        "numbers:read",
        "settings:read",
        "compliance:read",
      ],
    });

    const nav = await screen.findByRole("navigation", { name: "Settings" });
    expect(within(nav).getByText("Workspace")).toBeInTheDocument();
    expect(within(nav).getByText("Phone numbers")).toBeInTheDocument();
    expect(within(nav).queryByText("Developers")).not.toBeInTheDocument();
  });

  it("shows Developers when settings:write is permitted", async () => {
    renderSettings({ initialEntries: ["/settings/workspace"] });

    const nav = await screen.findByRole("navigation", { name: "Settings" });
    expect(within(nav).getByText("Developers")).toBeInTheDocument();
  });

  it("renders Team sub-tabs, updates the URL, and only mounts the active tab page", async () => {
    renderSettings({ initialEntries: ["/settings/team"] });

    expect(await screen.findByText("Team page")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Members & roles" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Security" })).toBeInTheDocument();
    expect(screen.queryByText("Security page")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "Security" }));

    expect(await screen.findByText("Security page")).toBeInTheDocument();
    expect(screen.queryByText("Team page")).not.toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/settings/team?tab=security");
  });

  it("redirects an unknown section to /settings/workspace without looping", async () => {
    renderSettings({ initialEntries: ["/settings/nope"] });

    expect(await screen.findByTestId("location")).toHaveTextContent("/settings/workspace");
    expect(screen.getByTestId("location")).toHaveTextContent("/settings/workspace");
    expect(await screen.findByRole("heading", { name: "Workspace" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Workspace" })).toHaveAttribute("aria-current", "page");
  });

  it("renders access denied without redirecting when a permitted section is missing", async () => {
    renderSettings({
      initialEntries: ["/settings/numbers"],
      permissions: ["org:read", "members:read", "settings:read"],
    });

    const nav = await screen.findByRole("navigation", { name: "Settings" });
    expect(nav).toBeInTheDocument();
    expect(screen.getByText("You do not have access to this setting.")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/settings/numbers");
  });

  it("renders the workspace name and setup pills", async () => {
    const view = renderSettings({ initialEntries: ["/settings/workspace"] });

    expect(await screen.findByText("Org Name")).toBeInTheDocument();
    expect(screen.getByText("org")).toBeInTheDocument();
    expect(screen.getByText("No provider yet")).toBeInTheDocument();
    expect(screen.getByText("No number yet")).toBeInTheDocument();
    expect(screen.getByText("1 member")).toBeInTheDocument();
    expect(screen.getByText("Not started")).toBeInTheDocument();

    view.unmount();

    renderSettings({ initialEntries: ["/settings/workspace"], memberCount: 2 });
    expect(await screen.findByText("2 members")).toBeInTheDocument();
  });

  it("disables the AI section's controls for a member without settings:write", async () => {
    renderSettings({
      initialEntries: ["/settings/ai?tab=providers"],
      permissions: ["org:read", "members:read", "settings:read"],
    });

    expect(
      await screen.findByText("You can view this, but only an admin can make changes here."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add a connection" })).toBeDisabled();
  });

  it("leaves the AI section's controls enabled when settings:write is permitted", async () => {
    renderSettings({ initialEntries: ["/settings/ai?tab=providers"] });

    expect(
      screen.queryByText("You can view this, but only an admin can make changes here."),
    ).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Add a connection" })).toBeEnabled();
  });

  it("renders a SpendCard stub per provider account", async () => {
    renderSettings({
      initialEntries: ["/settings/billing"],
      providerAccounts: [{ id: "pa1", provider: "twilio" }],
    });

    expect(await screen.findByText("Spend card twilio")).toBeInTheDocument();
  });

  it("renders the billing empty state when there are no provider accounts", async () => {
    renderSettings({ initialEntries: ["/settings/billing"] });

    expect(await screen.findByText("No spend yet")).toBeInTheDocument();
    expect(screen.getByText("Connect a provider to see what you are spending.")).toBeInTheDocument();
  });

  it("renders Billing sub-tabs and mounts the Dashboard page on the Dashboard tab", async () => {
    renderSettings({ initialEntries: ["/settings/billing"] });

    expect(await screen.findByRole("tab", { name: "Usage" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Dashboard" })).toBeInTheDocument();
    expect(screen.queryByText("Dashboard page")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "Dashboard" }));

    expect(await screen.findByText("Dashboard page")).toBeInTheDocument();
    expect(screen.queryByText("No spend yet")).not.toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/settings/billing?tab=dashboard");
  });

  it("lands directly on the Dashboard tab via ?tab=dashboard (the /dashboard redirect target)", async () => {
    renderSettings({ initialEntries: ["/settings/billing?tab=dashboard"] });

    expect(await screen.findByText("Dashboard page")).toBeInTheDocument();
  });
});
