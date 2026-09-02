import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DashboardPage } from "./DashboardPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";

// recharts' ResponsiveContainer requires a ResizeObserver, which jsdom does not provide.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

const OVERVIEW = {
  range: { start: "2026-08-01", end: "2026-08-29", days: 29 },
  messages: [
    { date: "2026-08-27", inbound: 4, outbound: 6, delivery_rate: 0.9 },
    { date: "2026-08-28", inbound: 2, outbound: 3, delivery_rate: null },
  ],
  calls: [{ date: "2026-08-28", calls: 5, avg_duration_seconds: 120 }],
  campaigns: [{ status: "running", count: 2 }],
  ai: [{ date: "2026-08-28", turns: 10, handoffs: 1 }],
};

const EMPTY_OVERVIEW = {
  range: { start: "2026-08-01", end: "2026-08-29", days: 29 },
  messages: [],
  calls: [],
  campaigns: [],
  ai: [],
};

const SPEND_SUMMARY = {
  total_micros: 12_000_000,
  total_usd: "$12.00",
  by_provider: {
    telnyx: {
      cost_micros: 8_000_000,
      by_metric: { sms_out: { quantity: 1000, cost_micros: 8_000_000 } },
      numbers: [],
    },
    bandwidth: {
      cost_micros: 4_000_000,
      by_metric: { sms_out: { quantity: 500, cost_micros: 4_000_000 } },
      numbers: [],
    },
  },
};

function spendDailyRows() {
  const now = new Date();
  return Array.from({ length: 30 }, (_, i) => {
    const day = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i));
    return {
      period_date: day.toISOString().slice(0, 10),
      provider: "telnyx",
      metric: "sms_out" as const,
      quantity: 1,
      cost_micros: 400_000,
    };
  });
}

const SPEND_DAILY = spendDailyRows();

function baseStubs(overrides: Record<string, unknown> = {}) {
  return {
    "/api/v1/spend/summary": SPEND_SUMMARY,
    "/api/v1/spend/daily": SPEND_DAILY,
    ...overrides,
  };
}

describe("DashboardPage", () => {
  it("renders the analytics series from the API", async () => {
    const client = makeStubClient(
      baseStubs({
        "/api/v1/analytics/overview": OVERVIEW,
      }),
    );
    renderWithProviders(<DashboardPage />, client);

    expect(await screen.findByText("Messages in / out")).toBeInTheDocument();
    expect(screen.getByText("Calls + avg duration")).toBeInTheDocument();
    expect(screen.getByText("AI turns + handoffs")).toBeInTheDocument();
    expect(screen.getByText("Campaign progress (current)")).toBeInTheDocument();

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/analytics/overview?days=30"),
      ).toBe(true),
    );
  });

  it("shows an empty state per chart when a series has no data", async () => {
    const client = makeStubClient(
      baseStubs({
        "/api/v1/analytics/overview": EMPTY_OVERVIEW,
      }),
    );
    renderWithProviders(<DashboardPage />, client);

    const emptyMessages = await screen.findAllByText("No data for this range.");
    expect(emptyMessages.length).toBeGreaterThan(0);
  });

  it("refetches with a different day range when the picker changes", async () => {
    const client = makeStubClient(
      baseStubs({
        "/api/v1/analytics/overview": OVERVIEW,
      }),
    );
    renderWithProviders(<DashboardPage />, client);

    await screen.findByText("Messages in / out");
    await userEvent.click(screen.getByRole("button", { name: "7d" }));

    await waitFor(() =>
      expect(
        client.calls.some((c) => c.path === "/api/v1/analytics/overview?days=7"),
      ).toBe(true),
    );
  });

  it("searches transcripts and renders matched segments", async () => {
    const client = makeStubClient(
      baseStubs({
        "/api/v1/analytics/overview": EMPTY_OVERVIEW,
        "/api/v1/search/transcripts": [
          {
            call_id: "call-1",
            contact_e164: "+19725550199",
            started_at: new Date().toISOString(),
            segments: [
              { role: "agent", text: "Hello there", at_ms: 0, matched: false },
              { role: "user", text: "I need a refund", at_ms: 1000, matched: true },
            ],
          },
        ],
      }),
    );
    renderWithProviders(<DashboardPage />, client);

    await screen.findByText("Transcript search");
    await userEvent.type(screen.getByLabelText("Search transcripts"), "refund");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText(/I need a refund/)).toBeInTheDocument();
    expect(
      client.calls.some((c) => c.path === "/api/v1/search/transcripts?q=refund"),
    ).toBe(true);
  });

  // P19: the spend tile - MTD total, per-provider breakdown, and a 30-day bar per day.
  it("renders the spend tile with MTD total and 30 daily bars", async () => {
    const client = makeStubClient(
      baseStubs({
        "/api/v1/analytics/overview": EMPTY_OVERVIEW,
      }),
    );
    renderWithProviders(<DashboardPage />, client);

    expect(await screen.findByText("$12.00")).toBeInTheDocument();
    expect(screen.getByText("telnyx")).toBeInTheDocument();
    // P19 fix-required #4: the bar's aria-label now carries the amount too.
    expect(
      screen.getAllByRole("img", { name: /^Spend \d{4}-\d{2}-\d{2}: /}),
    ).toHaveLength(30);
  });
});
