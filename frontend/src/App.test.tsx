import * as React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, useParams, useSearchParams } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import { AuthProvider, type Me } from "@/auth/AuthContext";
import { makeStubClient } from "@/test/harness";

const conversationsMock = vi.hoisted(() => ({ crash: false }));

vi.mock("@/softphone/SoftphoneProvider", () => ({
  SoftphoneProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  // P26: the rail's alerts bell reads the realtime socket through this hook. The mock
  // stubs the provider away entirely, which is exactly the "no socket" case the hook
  // exists for - it returns null and the bell falls back to polling.
  useOptionalSoftphone: () => null,
}));
vi.mock("@/softphone/SoftphonePanel", () => ({
  SoftphonePanel: () => null,
}));
vi.mock("@/pages/ConversationsPage", () => ({
  ConversationsPage: () => {
    if (conversationsMock.crash) throw new Error("page crashed");
    return <div>Inbox page</div>;
  },
}));
vi.mock("@/pages/ContactsPage", () => ({
  // Echoes ?tab= so the /lists redirect can be asserted to land on the Lists TAB, not
  // merely on the Contacts page.
  ContactsPage: () => {
    const [searchParams] = useSearchParams();
    const tab = searchParams.get("tab");
    return (
      <div>
        Contacts page
        {tab ? ` tab=${tab}` : ""}
      </div>
    );
  },
}));
vi.mock("@/pages/CallsPage", () => ({
  CallsPage: () => <div>Calls page</div>,
}));
vi.mock("@/pages/CampaignsPage", () => ({
  CampaignsPage: () => <div>Campaigns page</div>,
}));
vi.mock("@/pages/TeamPage", () => ({
  TeamPage: () => <div>Team page</div>,
}));
vi.mock("@/pages/SettingsSecurityPage", () => ({
  SettingsSecurityPage: () => <div>Security page</div>,
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
vi.mock("@/pages/PlatformPage", () => ({
  PlatformPage: () => <div>Platform page</div>,
}));
vi.mock("@/pages/LoginPage", () => ({
  LoginPage: () => <div>Login page</div>,
}));
vi.mock("@/pages/OrgPickerPage", () => ({
  OrgPickerPage: () => <div>Org picker page</div>,
}));
vi.mock("@/pages/AcceptInvitePage", () => ({
  AcceptInvitePage: () => <div>Accept invite page</div>,
}));
vi.mock("@/pages/settingsSections", () => ({
  SETTINGS_SECTIONS: [
    { id: "workspace", label: "Workspace", permission: "org:read" },
    { id: "team", label: "Team", permission: "members:read" },
    { id: "inboxes", label: "Departments & inboxes", permission: "inboxes:admin" },
    { id: "numbers", label: "Phone numbers", permission: "numbers:read" },
    { id: "providers", label: "Providers", permission: "settings:read" },
    { id: "messaging", label: "Messaging", permission: "compliance:read" },
    { id: "calling", label: "Calling", permission: "settings:read" },
    { id: "ai", label: "AI", permission: "settings:read" },
    { id: "billing", label: "Billing & usage", permission: "settings:read" },
    { id: "developers", label: "Developers", permission: "settings:write" },
  ],
}));
vi.mock("@/pages/SettingsPage", () => ({
  SettingsPage: () => {
    const { section } = useParams();
    const [searchParams] = useSearchParams();
    const tab = searchParams.get("tab");
    return (
      <div>
        Settings page {section}
        {tab ? ` tab=${tab}` : ""}
      </div>
    );
  },
}));

const ME: Me = {
  id: "u1",
  email: "a@example.com",
  full_name: "A",
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner" }],
};

function renderApp(initialEntries: string[] = ["/inbox"]) {
  const client = makeStubClient({
    "/api/v1/auth/me": ME,
    "/api/v1/inboxes": [],
    "/api/v1/me/capabilities": {
      permissions: ["contacts:read", "calls:read", "campaigns:read", "settings:read"],
      org: {
        has_provider: true,
        has_number: true,
        member_count: 2,
        registration_state: "approved",
      },
    },
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={initialEntries}>
          <App />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  conversationsMock.crash = false;
});

describe("App shell error boundary", () => {
  it("keeps the sidebar usable when a routed page throws", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    conversationsMock.crash = true;

    renderApp();

    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Sidebar" })).toBeInTheDocument();
  });
});

describe("App routes and legacy redirects", () => {
  it("/dashboard redirects to the Billing & usage Dashboard tab", async () => {
    renderApp(["/dashboard"]);

    expect(await screen.findByText("Settings page billing tab=dashboard")).toBeInTheDocument();
  });

  it("/settings lands on the first section the caller may open (settings:read → Providers)", async () => {
    renderApp(["/settings"]);

    expect(await screen.findByText("Settings page providers")).toBeInTheDocument();
  });

  it("/lists redirects to the Contacts Lists tab (P27 folded the page in)", async () => {
    renderApp(["/lists"]);

    expect(await screen.findByText("Contacts page tab=lists")).toBeInTheDocument();
  });

  it("/team lands on Team settings and /security lands on the Security tab", async () => {
    const teamView = renderApp(["/team"]);
    expect(await screen.findByText("Settings page team")).toBeInTheDocument();
    teamView.unmount();

    renderApp(["/security"]);
    expect(await screen.findByText("Settings page team tab=security")).toBeInTheDocument();
  });

  it("legacy settings routes land on their settings sections", async () => {
    const redirects = [
      { path: "/numbers", expected: "Settings page numbers" },
      { path: "/providers", expected: "Settings page providers" },
      { path: "/flows", expected: "Settings page calling tab=flows" },
      { path: "/queues", expected: "Settings page calling tab=queues" },
      { path: "/agent", expected: "Settings page ai" },
      { path: "/appointments", expected: "Settings page ai" },
      { path: "/platform", expected: "Settings page developers" },
    ];

    for (const { path, expected } of redirects) {
      const view = renderApp([path]);
      expect(await screen.findByText(expected)).toBeInTheDocument();
      view.unmount();
    }
  });

  it("/inbox/legacy redirects to the inbox and does not render the legacy page", async () => {
    renderApp(["/inbox/legacy"]);

    expect(await screen.findByText("Inbox page")).toBeInTheDocument();
    expect(screen.queryByText("Legacy inbox page")).not.toBeInTheDocument();
  });

  it("an unknown path lands on the inbox", async () => {
    renderApp(["/does-not-exist"]);

    expect(await screen.findByText("Inbox page")).toBeInTheDocument();
  });
});