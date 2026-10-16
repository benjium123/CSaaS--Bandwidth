import { describe, expect, it } from "vitest";

import { dayFigures, fmtMicros } from "./ProfitTab";

const day = {
  date: "2026-10-15",
  services: {
    calls: { charged: 162_000, carrier_cost: 144_000, ai_cost: 0, stripe_fees: 0, profit: 18_000, units: 18 },
  },
  unallocated_stripe_fees: 735_000,
  fixed_costs: 600_000,
  total: { charged: 162_000, carrier_cost: 144_000, ai_cost: 0, stripe_fees: 735_000, profit: -1_317_000 },
};

describe("ProfitTab figures", () => {
  it("All = the day total, with the server share and top-up fees", () => {
    const f = dayFigures(day, "all");
    expect(f.profit).toBe(-1_317_000);
    expect(f.fixed_costs).toBe(600_000);
  });
  it("one service shows only that service, without server cost", () => {
    const f = dayFigures(day, "calls");
    expect(f.profit).toBe(18_000);
    expect(f.units).toBe(18);
    expect(f.fixed_costs).toBe(0);
  });
  it("a service with nothing that day is zero, not missing", () => {
    expect(dayFigures(day, "fax")).toMatchObject({ charged: 0, profit: 0 });
  });
  it("formats micros as dollars with up to 3 decimals", () => {
    expect(fmtMicros(18_000)).toBe("$0.018");
    expect(fmtMicros(15_000_000)).toBe("$15.00");
    expect(fmtMicros(null)).toBe("—");
  });
});
