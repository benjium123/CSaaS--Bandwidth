/**
 * The inbox shows conversations, and the shell shows at most one banner.
 *
 * Both halves of this were the same operator complaint: opening /inbox put a setup
 * checklist and a stack of banners above the conversation columns and left them the bottom
 * half of the screen. The checklist moved to /setup; the banners now arbitrate.
 *
 * The "no checklist on /inbox" assertion is deliberately paired with a "/setup DOES render
 * it" assertion using the SAME capabilities payload. On its own the first would pass just
 * as happily if the checklist had been deleted, if the query stub were wrong, or if the
 * workspace happened to be fully set up - an absence proves nothing until you have shown
 * the thing can be present.
 */
import * as React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, type Me } from "@/auth/AuthContext";
import { makeStubClient } from "@/test/harness";
import { App } from "@/App";
import { BannerRegion } from "@/components/shell/BannerSlot";
import { MonitoringBanner } from "@/components/kyc/MonitoringBanner";
import { VerificationBanner } from "@/components/kyc/VerificationBanner";
import { LowBalanceBanner } from "@/components/billing/LowBalanceBanner";
import { PasskeyGraceBanner } from "@/components/security/PasskeyGraceBanner";

vi.mock("@/softphone/SoftphoneProvider", () => ({
  SoftphoneProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useOptionalSoftphone: () => null,
}));
vi.mock("@/softphone/SoftphonePanel", () => ({ SoftphonePanel: () => null }));
vi.mock("@/pages/ConversationsPage", () => ({
  ConversationsPage: () => <div>Inbox page</div>,
}));

const OWNER_PERMISSIONS = ["inbox:read", "org:read", "org:update", "settings:read"];

const ME: Me & { permissions: string[] } = {
  id: "u1",
  email: "owner@example.com",
  full_name: "Owner",
  permissions: OWNER_PERMISSIONS,
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner" }],
};

/** A brand-new workspace - the case the checklist exists for. */
const NEW_ORG = {
  has_provider: false,
  has_number: false,
  member_count: 1,
  registration_state: "none",
};

function renderApp(path: string, extraRoutes: Record<string, unknown> = {}) {
  const client = makeStubClient({
    "/api/v1/auth/me": ME,
    "/api/v1/me/capabilities": { permissions: OWNER_PERMISSIONS, org: NEW_ORG },
    "/api/v1/inboxes": [],
    "/api/v1/orgs/current": { id: "org-1", name: "Org", slug: "org" },
    // Must be stubbed SEPARATELY even though the line above is a prefix of it. The stub
    // matcher takes the longest matching key, and with no "/members" key declared the org
    // stub above is still the longest match - so AssignOwnerDrawer received the org OBJECT
    // where it expects an ARRAY and threw `.map is not a function`. The ErrorBoundary in
    // this very shell swallowed it, so the page rendered a crash and the suite stayed green.
    // A prefix stub answering a different sub-resource is invisible by construction.
    "/api/v1/orgs/current/members": [],
    "/api/v1/provider-accounts": [],
    // Everything that could put a banner in the shell is quiet unless a test says otherwise.
    "/api/v1/monitoring/status": { level: "normal", message: null, appealed_at: null },
    "/api/v1/kyc/profile": { status: "approved", missing: [] },
    "/api/v1/billing/summary": {
      balance_micros: 0,
      reserved_micros: 0,
      warning: null,
      auto_recharge: null,
      last_topup: null,
    },
    "/api/v1/notifications": [],
    ...extraRoutes,
  });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider client={client}>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("the inbox route shows conversations only", () => {
  it("renders no setup checklist on /inbox, for the very workspace that has one", async () => {
    renderApp("/inbox");

    expect(await screen.findByText("Inbox page")).toBeInTheDocument();
    // Give the capabilities query every chance to land and render a checklist. The rail and
    // the mobile bar both carry a Setup link, hence getAll.
    await waitFor(() => {
      expect(screen.getAllByRole("link", { name: "Setup" }).length).toBeGreaterThan(0);
    });
    expect(screen.queryByRole("list", { name: "Setup steps" })).not.toBeInTheDocument();
    expect(screen.queryByText("Finish setting up")).not.toBeInTheDocument();
  });

  it("but /setup renders it - the absence above is not vacuous", async () => {
    renderApp("/setup");

    expect(await screen.findByRole("list", { name: "Setup steps" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Setup" })).toBeInTheDocument();
    expect(screen.queryByText("Inbox page")).not.toBeInTheDocument();
  });

  it("/setup says so plainly once the workspace is finished, rather than rendering a void", async () => {
    renderApp("/setup", {
      "/api/v1/me/capabilities": {
        permissions: OWNER_PERMISSIONS,
        org: {
          has_provider: true,
          has_number: true,
          member_count: 3,
          registration_state: "approved",
        },
      },
    });

    expect(await screen.findByText("You are all set")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Setup steps" })).not.toBeInTheDocument();
  });
});

/**
 * Banner arbitration. Each case turns on SEVERAL conditions at once and asserts both that
 * exactly one banner rendered and WHICH one, so a regression that simply hid all of them
 * cannot pass.
 */
describe("at most one shell banner", () => {
  const PAUSED = {
    level: "paused",
    message: "Your traffic looks like a list you did not collect.",
    appealed_at: null,
  };
  const EMPTY_BALANCE = {
    balance_micros: 0,
    reserved_micros: 0,
    warning: "empty",
    auto_recharge: null,
    last_topup: null,
  };
  const DRAFT_KYC = { status: "draft", missing: [] };

  const PASSKEY_ME: Me & { permissions: string[] } = {
    ...ME,
    passkey_required: true,
    has_passkey: false,
    passkey_grace_until: new Date(Date.now() + 7 * 86_400_000).toISOString(),
  };

  function renderBanners(routes: Record<string, unknown>) {
    const client = makeStubClient({
      "/api/v1/auth/me": PASSKEY_ME,
      "/api/v1/me/capabilities": { permissions: OWNER_PERMISSIONS, org: NEW_ORG },
      "/api/v1/monitoring/status": { level: "normal", message: null, appealed_at: null },
      "/api/v1/kyc/profile": { status: "approved", missing: [] },
      "/api/v1/billing/summary": {
        balance_micros: 0,
        reserved_micros: 0,
        warning: null,
        auto_recharge: null,
        last_topup: null,
      },
      ...routes,
    });
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchInterval: false, gcTime: 0 } },
    });
    return render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider client={client}>
          <MemoryRouter>
            <BannerRegion>
              <MonitoringBanner />
              <LowBalanceBanner />
              <VerificationBanner />
              <PasskeyGraceBanner />
            </BannerRegion>
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );
  }

  beforeEach(() => {
    sessionStorage.clear();
  });

  it("shows monitoring alone when all four conditions are true at once", async () => {
    renderBanners({
      "/api/v1/monitoring/status": PAUSED,
      "/api/v1/billing/summary": EMPTY_BALANCE,
      "/api/v1/kyc/profile": DRAFT_KYC,
    });

    expect(await screen.findByText("Calling and texting are paused")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByRole("status")).toHaveLength(1);
    });
    expect(screen.queryByText("You are out of credits")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Verify your business to start calling and texting"),
    ).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Passkey required")).not.toBeInTheDocument();
  });

  it("falls to credits when monitoring is normal - credits outrank verification and passkey", async () => {
    renderBanners({
      "/api/v1/billing/summary": EMPTY_BALANCE,
      "/api/v1/kyc/profile": DRAFT_KYC,
    });

    expect(await screen.findByText("You are out of credits")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByRole("status")).toHaveLength(1);
    });
    expect(
      screen.queryByText("Verify your business to start calling and texting"),
    ).not.toBeInTheDocument();
  });

  it("falls to verification when there is credit, and to passkey when verified", async () => {
    const { unmount } = renderBanners({ "/api/v1/kyc/profile": DRAFT_KYC });

    expect(
      await screen.findByText("Verify your business to start calling and texting"),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByRole("status")).toHaveLength(1);
    });
    unmount();

    renderBanners({});
    expect(await screen.findByLabelText("Passkey required")).toBeInTheDocument();
    expect(screen.getAllByRole("status")).toHaveLength(1);
  });

  it("dismissing the winning credits banner lets the next one through", async () => {
    renderBanners({
      "/api/v1/billing/summary": EMPTY_BALANCE,
      "/api/v1/kyc/profile": DRAFT_KYC,
    });

    const dismiss = await screen.findByRole("button", { name: "Dismiss" });
    dismiss.click();

    expect(
      await screen.findByText("Verify your business to start calling and texting"),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("status")).toHaveLength(1);
  });
});

/**
 * ONE navigation per route.
 *
 * InboxColumn now renders the console reference's whole `.nav` block - brand, Search,
 * Notifications, Workspace, Lines - so on /inbox the 56px icon Sidebar beside it would be
 * a second copy of the same destinations. App.tsx hides the icon rail there and nowhere
 * else, and these two assertions are a pair on purpose: the absence on /inbox proves
 * nothing unless the same render shows it PRESENT on another console route.
 */
describe("the inbox route has exactly one navigation", () => {
  it("hides the icon Sidebar on /inbox", async () => {
    renderApp("/inbox");

    expect(await screen.findByText("Inbox page")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Sidebar" })).not.toBeInTheDocument();
  });

  it("keeps the icon Sidebar on every other console route", async () => {
    renderApp("/contacts");

    expect(
      await screen.findByRole("navigation", { name: "Sidebar" }),
    ).toBeInTheDocument();
  });

  it("leaves phone navigation alone - the mobile bar still renders on /inbox", async () => {
    renderApp("/inbox");

    // MobileTabBar is the rail on a phone and is outside the hidden branch entirely.
    expect(
      await screen.findByRole("navigation", { name: "Bottom navigation" }),
    ).toBeInTheDocument();
  });
});
