import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { UsageTable } from "./UsageTable";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";
import type { UsageSummary } from "@/api/billing";

const CAPABILITIES = {
  permissions: ["org:billing"],
  org: {
    has_provider: true,
    has_number: true,
    member_count: 2,
    registration_state: "approved",
  },
};

const CALL_ROW = {
  call_id: "call-1",
  occurred_at: "2026-01-02T03:04:05Z",
  price_micros: 1_000_000,
  seconds: 120,
};

const CALL_DETAIL = {
  call_id: "call-1",
  total_price_micros: 50_000,
  events: [{ id: "ev-1", metric: "llm_tokens_in", quantity: 1000, price_micros: 50_000 }],
  occurred_at: "2026-01-02T03:04:05Z",
  seconds: 120,
};

function makeUsage(overrides: Partial<UsageSummary> = {}): UsageSummary {
  return {
    total_price_micros: 19_150,
    by_metric: [
      { metric: "ai_voice_seconds", quantity: 750, price_micros: 18_000 },
      { metric: "stt_seconds", quantity: 600, price_micros: 400 },
      { metric: "tts_characters", quantity: 1000, price_micros: 500 },
      { metric: "llm_tokens_in", quantity: 2000, price_micros: 100 },
      { metric: "llm_tokens_out", quantity: 3000, price_micros: 150 },
    ],
    calls: [CALL_ROW],
    ...overrides,
  };
}

// The stub matches via path.startsWith over Object.keys order. Declaring the call-detail
// prefix BEFORE the broader usage prefix keeps "/api/v1/billing/usage/calls/call-1" from
// being answered by the summary stub.
function baseRoutes(overrides: Record<string, RouteStub | unknown> = {}) {
  return {
    "/api/v1/me/capabilities": CAPABILITIES,
    "/api/v1/billing/usage/calls": CALL_DETAIL,
    "/api/v1/billing/usage": makeUsage(),
    ...overrides,
  };
}

describe("UsageTable", () => {
  it("renders one row per metric with its plain label", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<UsageTable />, client);

    expect(await screen.findByText("Assistant minutes")).toBeInTheDocument();
    expect(screen.getByText("Speech recognition")).toBeInTheDocument();
    expect(screen.getByText("Voice generation")).toBeInTheDocument();
    expect(screen.getByText("Language model input")).toBeInTheDocument();
    expect(screen.getByText("Language model output")).toBeInTheDocument();
    expect(screen.queryByText("ai_voice_seconds")).not.toBeInTheDocument();
  });

  it("renders seconds as minutes", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<UsageTable />, client);

    expect(await screen.findByText("12.5 minutes")).toBeInTheDocument();
  });

  it("renders prices as dollars and never the raw micros digits", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/usage": makeUsage({
          total_price_micros: 1_250_000,
          by_metric: [
            { metric: "ai_voice_seconds", quantity: 750, price_micros: 1_250_000 },
          ],
          calls: [],
        }),
      }),
    );
    renderWithProviders(<UsageTable />, client);

    // A single line means the row and the total row carry the SAME dollar string, so this
    // has to be findAllByText - getByText would fail on the ambiguity rather than on the
    // thing being asserted.
    expect((await screen.findAllByText("$1.25")).length).toBeGreaterThan(0);
    expect(document.body.textContent).not.toContain("1250000");
  });

  it("shows the summary total in the footer", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/usage": makeUsage({
          total_price_micros: 49_000,
          by_metric: [
            { metric: "ai_voice_seconds", quantity: 60, price_micros: 49_000 },
          ],
          calls: [],
        }),
      }),
    );
    renderWithProviders(<UsageTable />, client);

    const table = await screen.findByRole("table");
    const tfoot = table.querySelector("tfoot") as HTMLElement;
    expect(within(tfoot).getByText("$0.05")).toBeInTheDocument();
  });

  it("renders the object-map shape of by_metric the same as the array shape", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/usage": makeUsage({
          total_price_micros: 18_000,
          by_metric: {
            ai_voice_seconds: { quantity: 750, price_micros: 18_000 },
          },
          calls: [],
        }),
      }),
    );
    renderWithProviders(<UsageTable />, client);

    expect(await screen.findByText("Assistant minutes")).toBeInTheDocument();
    expect(screen.getByText("12.5 minutes")).toBeInTheDocument();
  });

  it("ignores cost_micros on every line and renders only price", async () => {
    const linesWithCost = [
      {
        metric: "ai_voice_seconds",
        quantity: 60,
        price_micros: 1_990_000,
        cost_micros: 9_990_000,
      },
    ];
    const usage: UsageSummary = {
      total_price_micros: 1_990_000,
      by_metric: linesWithCost,
      calls: [],
    };
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/usage": usage,
      }),
    );
    renderWithProviders(<UsageTable />, client);

    expect((await screen.findAllByText("$1.99")).length).toBeGreaterThan(0);
    expect(screen.queryByText("$9.99")).not.toBeInTheDocument();
    // The cost is not merely un-formatted somewhere: its digits never reach the DOM.
    expect(document.body.textContent).not.toContain("9990000");
  });

  it("renders the empty state when there are no usage lines", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/usage": makeUsage({
          total_price_micros: 0,
          by_metric: [],
          calls: [],
        }),
      }),
    );
    renderWithProviders(<UsageTable />, client);

    expect(await screen.findByText("Nothing used yet.")).toBeInTheDocument();
    expect(
      screen.getByText("Usage appears here as soon as your assistant takes a call."),
    ).toBeInTheDocument();
  });

  it("opens the drawer and fetches per-call details on click", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<UsageTable />, client);

    await screen.findByText("Assistant minutes");
    await userEvent.click(screen.getByRole("button", { name: /^Call on/ }));

    expect(await screen.findByText("1,000 tokens")).toBeInTheDocument();
    expect(screen.getByText("Total for this call")).toBeInTheDocument();
    expect(
      client.calls.some((call) => call.path === "/api/v1/billing/usage/calls/call-1"),
    ).toBe(true);
  });

  it("does not fetch call details before a call row is clicked", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<UsageTable />, client);

    await screen.findByText("Assistant minutes");
    expect(
      client.calls.some((call) => call.path.startsWith("/api/v1/billing/usage/calls/")),
    ).toBe(false);
  });

  it("shows a usage error with a retry that recovers", async () => {
    let usageCalls = 0;
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/usage": ((_path, _init) => {
          usageCalls += 1;
          if (usageCalls === 1) return new Error("usage down");
          return makeUsage();
        }) as RouteStub,
      }),
    );
    renderWithProviders(<UsageTable />, client);

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("usage down")).toBeInTheDocument();

    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Assistant minutes")).toBeInTheDocument();
  });
});
