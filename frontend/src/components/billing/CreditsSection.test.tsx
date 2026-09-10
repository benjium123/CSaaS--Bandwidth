import { beforeEach, describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { CreditsSection } from "./CreditsSection";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const CAPABILITIES = {
  permissions: ["settings:read", "org:billing"],
  org: {
    has_provider: true,
    has_number: true,
    member_count: 2,
    registration_state: "approved",
  },
};

const SUMMARY = {
  balance_micros: 42_000_000,
  reserved_micros: 0,
  warning: null,
  auto_recharge: null,
  last_topup: 100_000_000,
};

function routes() {
  return {
    "/api/v1/me/capabilities": CAPABILITIES,
    "/api/v1/billing/summary": SUMMARY,
    "/api/v1/billing/usage/calls": { call_id: "c1", events: [] },
    "/api/v1/billing/usage": { by_metric: [], calls: [] },
    "/api/v1/billing/ledger": { items: [], next_cursor: null },
    "/api/v1/billing/rates": [],
    "/api/v1/billing/payment-methods": [],
  };
}

describe("CreditsSection", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it("stacks the balance, usage, prices and payment sections", async () => {
    renderWithProviders(<CreditsSection />, makeStubClient(routes()));

    await waitFor(() => expect(screen.getByText("$42.00")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: "Usage this month" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Prices" })).toBeInTheDocument();
  });

  it("keeps the credit history collapsed until it is opened", async () => {
    const client = makeStubClient(routes());
    renderWithProviders(<CreditsSection />, client);

    await waitFor(() => expect(screen.getByText("$42.00")).toBeInTheDocument());

    // Collapsed means not mounted, so the ledger must not have been fetched at all - a
    // closed section that still hits the API is the bug this asserts against.
    expect(client.calls.some((call) => call.path.startsWith("/api/v1/billing/ledger"))).toBe(
      false,
    );

    const toggle = screen.getByRole("button", { name: "Credit history" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("does not mount the platform operator section on the customer surface", async () => {
    renderWithProviders(<CreditsSection />, makeStubClient(routes()));

    await waitFor(() => expect(screen.getByText("$42.00")).toBeInTheDocument());
    expect(screen.queryByText("For platform operators.")).not.toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toMatch(/margin/i);
  });
});
