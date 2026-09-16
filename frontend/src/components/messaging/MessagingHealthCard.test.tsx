import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { MessagingHealthCard } from "./MessagingHealthCard";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const BASE_HEALTH = {
  window_start: "2026-08-01",
  window_end: "2026-08-29",
  volume: 640,
  delivery_rate: 0.9449,
  spam_block_rate: 0.033,
  opt_out_rate: 0.011,
  failed_by_class: {
    spam_blocked: 5,
    carrier_rejected: 2,
    invalid_destination: 1,
    opted_out: 0,
    unknown: 1,
  },
  level: "ok",
  reasons: [],
  thresholds: {
    delivery_warn: 0.95,
    delivery_critical: 0.9,
    spam_warn: 0.05,
    spam_critical: 0.1,
    opt_out_warn: 0.02,
    opt_out_critical: 0.05,
    min_volume: 100,
  },
};

describe("MessagingHealthCard", () => {
  it("renders three rates and the level badge", async () => {
    const client = makeStubClient({
      "/api/v1/analytics/health?days=7": BASE_HEALTH,
    });
    renderWithProviders(<MessagingHealthCard days={7} />, client);

    expect(await screen.findByText("Healthy")).toBeInTheDocument();
    expect(screen.getByText("94.5%")).toBeInTheDocument();
    expect(screen.getByText("3.3%")).toBeInTheDocument();
    expect(screen.getByText("1.1%")).toBeInTheDocument();
  });

  it("renders no-data state without percentages", async () => {
    const client = makeStubClient({
      "/api/v1/analytics/health?days=7": {
        ...BASE_HEALTH,
        level: "no_data",
        volume: 0,
        delivery_rate: null,
        spam_block_rate: null,
        opt_out_rate: null,
        reasons: [],
        failed_by_class: {
          spam_blocked: 0,
          carrier_rejected: 0,
          invalid_destination: 0,
          opted_out: 0,
          unknown: 0,
        },
      },
    });
    renderWithProviders(<MessagingHealthCard days={7} />, client);

    expect(await screen.findByText("No text traffic in this range yet.")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
    expect(screen.queryByText("—")).not.toBeInTheDocument();
  });

  it("renders the reasons list when present", async () => {
    const client = makeStubClient({
      "/api/v1/analytics/health?days=7": {
        ...BASE_HEALTH,
        level: "warn",
        reasons: ["Delivery rate has dropped below 95% for 2 consecutive days."],
      },
    });
    renderWithProviders(<MessagingHealthCard days={7} />, client);

    expect(
      await screen.findByText("Delivery rate has dropped below 95% for 2 consecutive days."),
    ).toBeInTheDocument();
  });
});
