import { beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LowBalanceBanner } from "./LowBalanceBanner";
import { makeStubClient, renderWithProviders } from "@/test/harness";

// The banner gates on OWNERSHIP now (the summary endpoint is owner-only server-side), so
// every render needs /auth/me. makeStubClient pins auth.orgId to "org-1", so the owner's
// membership MUST name org-1 or isOwner() fails closed and nothing ever renders.
const OWNER_ME = {
  id: "u1",
  email: "owner@example.com",
  full_name: "Owner",
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner" }],
};

const CAPABILITIES = {
  permissions: ["settings:read"],
  org: {
    has_provider: true,
    has_number: true,
    member_count: 2,
    registration_state: "approved",
  },
};

function summary(warning: "low" | "critical" | "empty" | null) {
  return {
    balance_micros: 0,
    reserved_micros: 0,
    warning,
    auto_recharge: null,
    last_topup: null,
  };
}

/** The owner identity under the caller's routes (which can override /auth/me for the
 *  non-owner and unknown-`me` cases). */
function makeBannerClient(routes: Record<string, unknown> = {}) {
  return makeStubClient({
    "/api/v1/auth/me": OWNER_ME,
    ...routes,
  });
}

beforeEach(() => {
  sessionStorage.clear();
});

describe("LowBalanceBanner", () => {
  it("renders the low-balance warning", async () => {
    const client = makeBannerClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("low"),
    });
    renderWithProviders(<LowBalanceBanner />, client);

    expect(await screen.findByText("Your credits are running low")).toBeInTheDocument();
    expect(
      screen.getByText("Add credits so your assistant keeps answering."),
    ).toBeInTheDocument();
  });

  it("shows a modal, not the slim banner, for a prepaid workspace at empty", async () => {
    const client = makeBannerClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": { ...summary("empty"), telephony_prepaid: true },
    });
    renderWithProviders(<LowBalanceBanner />, client);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Your balance is empty")).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "Outgoing texts and calls are paused and incoming calls are being declined until you add credit or buy a bundle.",
      ),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Add credit" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Buy bundles" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Not now" })).toBeInTheDocument();
  });

  it("dismisses the empty-balance modal for this session on Not now", async () => {
    const client = makeBannerClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": { ...summary("empty"), telephony_prepaid: true },
    });
    renderWithProviders(<LowBalanceBanner />, client);

    await screen.findByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "Not now" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(sessionStorage.getItem("csaas.billing.banner.dismissed")).toBe("empty");
  });

  it("keeps the assistant copy at empty when texting and calling are not prepaid", async () => {
    const client = makeBannerClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": { ...summary("empty"), telephony_prepaid: false },
    });
    renderWithProviders(<LowBalanceBanner />, client);

    expect(
      await screen.findByText(
        "Your assistant is not answering and campaigns are paused until you add credits.",
      ),
    ).toBeInTheDocument();
  });

  it("renders nothing when warning is null", async () => {
    const client = makeBannerClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary(null),
    });
    renderWithProviders(<LowBalanceBanner />, client);

    await waitFor(() => {
      expect(client.calls.some((call) => call.path === "/api/v1/billing/summary")).toBe(true);
    });
    expect(screen.queryByText("Your credits are running low")).not.toBeInTheDocument();
  });

  it("dismisses and writes the dismissed level to sessionStorage", async () => {
    const client = makeBannerClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("low"),
    });
    renderWithProviders(<LowBalanceBanner />, client);

    await screen.findByText("Your credits are running low");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(screen.queryByText("Your credits are running low")).not.toBeInTheDocument();
    expect(sessionStorage.getItem("csaas.billing.banner.dismissed")).toBe("low");
  });

  it("stays hidden for low but shows for critical after dismissing low", async () => {
    const firstClient = makeBannerClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("low"),
    });
    const first = renderWithProviders(<LowBalanceBanner />, firstClient);
    await screen.findByText("Your credits are running low");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    first.unmount();

    const secondClient = makeBannerClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("low"),
    });
    const second = renderWithProviders(<LowBalanceBanner />, secondClient);
    await waitFor(() => {
      expect(secondClient.calls.some((call) => call.path === "/api/v1/billing/summary")).toBe(true);
    });
    expect(screen.queryByText("Your credits are running low")).not.toBeInTheDocument();
    second.unmount();

    const thirdClient = makeBannerClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("critical"),
    });
    renderWithProviders(<LowBalanceBanner />, thirdClient);
    expect(await screen.findByText("You are almost out of credits")).toBeInTheDocument();
  });

  it("renders nothing and makes no billing request for a non-owner, even one holding settings:read", async () => {
    const client = makeBannerClient({
      // Same identity, non-owner role. CAPABILITIES below still grants settings:read -
      // the permission string is no longer what decides this.
      "/api/v1/auth/me": {
        ...OWNER_ME,
        memberships: [{ ...OWNER_ME.memberships[0], role_name: "admin" }],
      },
      "/api/v1/me/capabilities": CAPABILITIES,
      // Stubbed so the test fails loudly if the request were made at all.
      "/api/v1/billing/summary": summary("low"),
    });
    renderWithProviders(<LowBalanceBanner />, client);

    // Wait for the identity lookup so the assertion is not just "we checked too early".
    await waitFor(() => {
      expect(client.calls.some((call) => call.path === "/api/v1/auth/me")).toBe(true);
    });
    expect(screen.queryByText("Your credits are running low")).not.toBeInTheDocument();
    expect(client.calls.some((call) => call.path === "/api/v1/billing/summary")).toBe(false);
  });

  it("makes no billing request while `me` is still unknown", async () => {
    const client = makeBannerClient({
      "/api/v1/auth/me": new Error("unauthorized"),
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("low"),
    });
    renderWithProviders(<LowBalanceBanner />, client);

    // Wait for the identity lookup to have happened before asserting - otherwise "no
    // summary request" would only mean we looked too early.
    await waitFor(() => {
      expect(client.calls.some((call) => call.path === "/api/v1/auth/me")).toBe(true);
    });
    expect(client.calls.some((call) => call.path === "/api/v1/billing/summary")).toBe(false);
  });

  it("has an Add credits button that does not crash when clicked", async () => {
    const client = makeBannerClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("low"),
    });
    renderWithProviders(<LowBalanceBanner />, client);

    const button = await screen.findByRole("button", { name: "Add credits" });
    await userEvent.click(button);
    expect(screen.getByRole("button", { name: "Add credits" })).toBeInTheDocument();
  });

  it("never renders the word micros", async () => {
    const client = makeBannerClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("critical"),
    });
    renderWithProviders(<LowBalanceBanner />, client);

    await screen.findByText("You are almost out of credits");
    expect(document.body.textContent).not.toContain("micros");
  });
});
