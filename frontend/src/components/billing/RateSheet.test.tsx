import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RateSheet } from "./RateSheet";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

const CAPABILITIES = {
  permissions: ["org:billing"],
  org: {
    has_provider: true,
    has_number: true,
    member_count: 2,
    registration_state: "approved",
  },
};

function baseRoutes(overrides: Record<string, RouteStub | unknown> = {}) {
  return {
    "/api/v1/me/capabilities": CAPABILITIES,
    "/api/v1/billing/rates": [
      { metric: "ai_voice_seconds", price_micros: 300 },
      { metric: "tts_characters", price_micros: 5_000 },
    ],
    ...overrides,
  };
}

describe("RateSheet", () => {
  it("renders plain metric labels and a per-minute price scaled from per-second price", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<RateSheet />, client);

    expect(await screen.findByText("Assistant minutes")).toBeInTheDocument();
    expect(screen.getByText(/\$0\.018/)).toBeInTheDocument();
    expect(screen.getByText(/per minute/)).toBeInTheDocument();
  });

  it("renders a per-1,000-characters row", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<RateSheet />, client);

    expect(await screen.findByText("Voice generation")).toBeInTheDocument();
    expect(screen.getByText(/per 1,000 characters/)).toBeInTheDocument();
  });

  it("ignores cost_micros even when the payload carries it", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/rates": [
          { metric: "ai_voice_seconds", price_micros: 1_000, cost_micros: 9_990_000 },
        ],
      }),
    );
    renderWithProviders(<RateSheet />, client);

    expect(await screen.findByText(/\$0\.06/)).toBeInTheDocument();
    expect(screen.queryByText(/\$9\.99/)).not.toBeInTheDocument();
  });

  it("shows an error with a retry that recovers", async () => {
    let ratesCalls = 0;
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/rates": ((_path, _init) => {
          ratesCalls += 1;
          if (ratesCalls === 1) return new Error("rates down");
          return [
            { metric: "ai_voice_seconds", price_micros: 300 },
            { metric: "tts_characters", price_micros: 5_000 },
          ];
        }) as RouteStub,
      }),
    );
    renderWithProviders(<RateSheet />, client);

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("rates down")).toBeInTheDocument();

    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Assistant minutes")).toBeInTheDocument();
  });

  it("renders the empty state", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/rates": [],
      }),
    );
    renderWithProviders(<RateSheet />, client);

    expect(await screen.findByText("Prices are not available yet.")).toBeInTheDocument();
  });
});
