import { beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LowBalanceBanner } from "./LowBalanceBanner";
import { makeStubClient, renderWithProviders } from "@/test/harness";

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

beforeEach(() => {
  sessionStorage.clear();
});

describe("LowBalanceBanner", () => {
  it("renders the low-balance warning", async () => {
    const client = makeStubClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("low"),
    });
    renderWithProviders(<LowBalanceBanner />, client);

    expect(await screen.findByText("Your credits are running low")).toBeInTheDocument();
    expect(
      screen.getByText("Add credits so your assistant keeps answering."),
    ).toBeInTheDocument();
  });

  it("tells a prepaid workspace at empty that texting and calling are paused", async () => {
    const client = makeStubClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": { ...summary("empty"), telephony_prepaid: true },
    });
    renderWithProviders(<LowBalanceBanner />, client);

    expect(await screen.findByText("You are out of credits")).toBeInTheDocument();
    expect(
      screen.getByText("Texting and outbound calling are paused until you add credits."),
    ).toBeInTheDocument();
  });

  it("keeps the assistant copy at empty when texting and calling are not prepaid", async () => {
    const client = makeStubClient({
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
    const client = makeStubClient({
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
    const client = makeStubClient({
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
    const firstClient = makeStubClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("low"),
    });
    const first = renderWithProviders(<LowBalanceBanner />, firstClient);
    await screen.findByText("Your credits are running low");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    first.unmount();

    const secondClient = makeStubClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("low"),
    });
    const second = renderWithProviders(<LowBalanceBanner />, secondClient);
    await waitFor(() => {
      expect(secondClient.calls.some((call) => call.path === "/api/v1/billing/summary")).toBe(true);
    });
    expect(screen.queryByText("Your credits are running low")).not.toBeInTheDocument();
    second.unmount();

    const thirdClient = makeStubClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("critical"),
    });
    renderWithProviders(<LowBalanceBanner />, thirdClient);
    expect(await screen.findByText("You are almost out of credits")).toBeInTheDocument();
  });

  it("renders nothing and makes no billing request without settings read permission", async () => {
    const client = makeStubClient({
      "/api/v1/me/capabilities": {
        ...CAPABILITIES,
        permissions: [],
      },
      "/api/v1/billing/summary": summary("low"),
    });
    renderWithProviders(<LowBalanceBanner />, client);

    await waitFor(() => {
      expect(client.calls.some((call) => call.path === "/api/v1/me/capabilities")).toBe(true);
    });
    expect(screen.queryByText("Your credits are running low")).not.toBeInTheDocument();
    expect(client.calls.some((call) => call.path === "/api/v1/billing/summary")).toBe(false);
  });

  it("has an Add credits button that does not crash when clicked", async () => {
    const client = makeStubClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("low"),
    });
    renderWithProviders(<LowBalanceBanner />, client);

    const button = await screen.findByRole("button", { name: "Add credits" });
    await userEvent.click(button);
    expect(screen.getByRole("button", { name: "Add credits" })).toBeInTheDocument();
  });

  it("never renders the word micros", async () => {
    const client = makeStubClient({
      "/api/v1/me/capabilities": CAPABILITIES,
      "/api/v1/billing/summary": summary("critical"),
    });
    renderWithProviders(<LowBalanceBanner />, client);

    await screen.findByText("You are almost out of credits");
    expect(document.body.textContent).not.toContain("micros");
  });
});
