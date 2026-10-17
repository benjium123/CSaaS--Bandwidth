import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { DashboardTab } from "@/components/ops/DashboardTab";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const PROFIT_DAILY = {
  start: "2026-10-03",
  end: "2026-10-16",
  org_id: null,
  org_name: null,
  services: [],
  days: [
    {
      date: "2026-10-15",
      services: {},
      unallocated_stripe_fees: 0,
      fixed_costs: 0,
      total: { charged: 10_000_000, carrier_cost: 3_000_000, ai_cost: 0, stripe_fees: 400_000, profit: 5_000_000 },
    },
    {
      date: "2026-10-16",
      services: {},
      unallocated_stripe_fees: 0,
      fixed_costs: 0,
      total: { charged: 12_500_000, carrier_cost: 4_000_000, ai_cost: 0, stripe_fees: 500_000, profit: 8_000_000 },
    },
  ],
  notes: [],
};

const ORGS = {
  orgs: [
    {
      org_id: "org-a",
      name: "Acme Telecom",
      metrics: { accrual_revenue: 20_000_000, provider_cost: 8_000_000, stripe_fees: 600_000, accrual_profit: 11_400_000 },
    },
    {
      org_id: "org-b",
      name: "Beacon Voice",
      metrics: { accrual_revenue: 5_000_000, provider_cost: 2_000_000, stripe_fees: 150_000, accrual_profit: 2_850_000 },
    },
  ],
};

const GUARD = {
  anchor_balance_micros: 11_990_000,
  anchor_at: null,
  spent_micros: 0,
  estimate_micros: 8_700_000,
  checked_at: null,
  paused: false,
  warn_sent_at: null,
  stop_sent_at: null,
  warn_micros: 6_000_000,
  stop_micros: 5_250_000,
};

const ALL_SECTIONS = ["queue", "alerts", "texting", "purchases", "profit", "console", "support", "website"];
const NO_COUNTS = { queue: 0, alerts: 0, texting: 0, purchases: 0 };

/**
 * makeStubClient matches on `path.startsWith(key)`, longest key first - so the query strings on
 * profit-daily / orgs / support still resolve to their stub, and "/" answers AuthProvider.
 */
function stubClient(overrides: Record<string, unknown> = {}) {
  return makeStubClient({
    "/api/v1/ops/console/support": { requests: [{}, {}] },
    "/api/v1/ops/site/chats/unread": { count: 1 },
    "/api/v1/ops/console/signalwire-guard": GUARD,
    "/api/v1/ops/console/telnyx": { balance: { balance: "412.18", currency: "USD" } },
    "/api/v1/ops/console/orgs": ORGS,
    "/api/v1/ops/console/profit-daily": PROFIT_DAILY,
    "/": {},
    ...overrides,
  });
}

describe("DashboardTab", () => {
  it("shows today's revenue, the day-on-day move, the balances and the stop level", async () => {
    const client = stubClient();
    renderWithProviders(<DashboardTab counts={NO_COUNTS} sections={ALL_SECTIONS} onNavigate={() => {}} />, client);

    expect(await screen.findByText("$12.50")).toBeTruthy();
    expect(screen.getByText(/▲ 25%/)).toBeTruthy();
    expect(await screen.findByText("$412.18")).toBeTruthy();
    expect(await screen.findByText("$8.70")).toBeTruthy();
    expect(screen.getByText("pauses at $5.25")).toBeTruthy();
  });

  it("moves to the queue when the Review queue card is clicked", async () => {
    const client = stubClient();
    const onNavigate = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<DashboardTab counts={{ queue: 3 }} sections={ALL_SECTIONS} onNavigate={onNavigate} />, client);

    await user.click(await screen.findByRole("button", { name: /Review queue/ }));

    expect(onNavigate).toHaveBeenCalledWith("queue");
  });

  it("never asks for money data when the profit section is not granted", async () => {
    const client = stubClient();
    renderWithProviders(
      <DashboardTab
        counts={NO_COUNTS}
        sections={["queue", "alerts", "texting", "purchases", "support", "website"]}
        onNavigate={() => {}}
      />,
      client,
    );

    await waitFor(() =>
      expect(client.calls.some((call) => call.path.startsWith("/api/v1/ops/site/chats/unread"))).toBe(true),
    );
    await waitFor(() =>
      expect(client.calls.some((call) => call.path.startsWith("/api/v1/ops/console/support"))).toBe(true),
    );

    expect(client.calls.some((call) => call.path.includes("/console/profit-daily"))).toBe(false);
    expect(client.calls.some((call) => call.path.includes("/console/orgs"))).toBe(false);
    expect(client.calls.some((call) => call.path.includes("/console/telnyx"))).toBe(false);
    expect(client.calls.some((call) => call.path.includes("/console/signalwire-guard"))).toBe(false);
    expect(screen.queryByText("Revenue today")).toBeNull();
  });

  it("lists the work that needs action", async () => {
    const client = stubClient();
    renderWithProviders(<DashboardTab counts={{ queue: 3 }} sections={ALL_SECTIONS} onNavigate={() => {}} />, client);

    expect(await screen.findByText("3 waiting in the review queue")).toBeTruthy();
    expect(await screen.findByText("2 open support requests")).toBeTruthy();
  });

  it("says nothing is waiting when every count is zero", async () => {
    const client = stubClient({
      "/api/v1/ops/console/support": { requests: [] },
      "/api/v1/ops/site/chats/unread": { count: 0 },
    });
    renderWithProviders(<DashboardTab counts={NO_COUNTS} sections={ALL_SECTIONS} onNavigate={() => {}} />, client);

    expect(await screen.findByText("Nothing is waiting on you.")).toBeTruthy();
  });

  it("lists both customers in the monthly chart, biggest revenue first", async () => {
    const client = stubClient();
    renderWithProviders(<DashboardTab counts={NO_COUNTS} sections={ALL_SECTIONS} onNavigate={() => {}} />, client);

    const first = await screen.findByText("Acme Telecom");
    const second = await screen.findByText("Beacon Voice");
    expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
