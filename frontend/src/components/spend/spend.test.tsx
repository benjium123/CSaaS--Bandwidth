import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SpendCard, SpendTile } from "./SpendCard";
import { RatesDrawer } from "./RatesDrawer";
import { daysInRange, dollarsToMicros, formatMicros, lastNDaysRange, todayUTC } from "@/api/spend";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const SUMMARY = {
  total_micros: 12_000_000,
  total_usd: "$12.00",
  by_provider: {
    telnyx: {
      cost_micros: 8_000_000,
      by_metric: {
        sms_out: { quantity: 1000, cost_micros: 7_000_000 },
      },
      numbers: [{ number_id: "n1", e164: "+12145550100", cost_micros: 1_000_000 }],
    },
    bandwidth: {
      cost_micros: 1_234_567,
      by_metric: {
        sms_out: { quantity: 10, cost_micros: 7_900 },
      },
      numbers: [],
    },
  },
};

// telnyx/sms_out and telnyx/voice_min_out are NOT overridden (unit_cost_micros equals its
// own default). telnyx/number_mrc IS overridden - unit_cost_micros ($0.30) differs from
// default_unit_cost_micros ($0.25) - this is the row the Reset test targets.
const RATES = [
  {
    provider: "telnyx",
    metric: "sms_out",
    unit_cost_micros: 4000,
    default_unit_cost_micros: 4000,
    is_override: false,
    currency: "USD",
  },
  {
    provider: "telnyx",
    metric: "voice_min_out",
    unit_cost_micros: 12000,
    default_unit_cost_micros: 12000,
    is_override: false,
    currency: "USD",
  },
  {
    provider: "telnyx",
    metric: "number_mrc",
    unit_cost_micros: 300_000,
    default_unit_cost_micros: 250_000,
    is_override: true,
    currency: "USD",
  },
];

function ratesRoute(_path: string, init?: RequestInit) {
  if (init?.method === "PUT") {
    return { day: todayUTC() };
  }
  return RATES;
}

// Aligned to the SAME real "now" that SpendTile's own lastNDaysRange(30) computes, so the
// zero-fill in SpendTile and these fixture dates land on the same 30 calendar days.
function dailyRows() {
  const range = lastNDaysRange(30);
  return daysInRange(range.from, range.to).map((day) => ({
    period_date: day,
    provider: "telnyx",
    metric: "sms_out" as const,
    quantity: 1,
    cost_micros: 1_000_000,
  }));
}

// Only every other day has a row - SpendTile must still zero-fill the rest so exactly 30
// bars always render, half of them at 0 height.
function gappedDailyRows() {
  const range = lastNDaysRange(30);
  return daysInRange(range.from, range.to)
    .filter((_, i) => i % 2 === 0)
    .map((day) => ({
      period_date: day,
      provider: "telnyx",
      metric: "sms_out" as const,
      quantity: 1,
      cost_micros: 1_000_000,
    }));
}

// All 30 real days plus one row dated outside the requested range (e.g. a backend
// clock-skew bug) - the out-of-range row must be dropped, not appended as a 31st bar.
function dailyRowsWithOutOfRangeExtra() {
  const range = lastNDaysRange(30);
  const rows = daysInRange(range.from, range.to).map((day) => ({
    period_date: day,
    provider: "telnyx",
    metric: "sms_out" as const,
    quantity: 1,
    cost_micros: 1_000_000,
  }));
  const outOfRange = new Date(`${range.from}T00:00:00Z`);
  outOfRange.setUTCDate(outOfRange.getUTCDate() - 10);
  rows.push({
    period_date: outOfRange.toISOString().slice(0, 10),
    provider: "telnyx",
    metric: "sms_out" as const,
    quantity: 1,
    cost_micros: 9_000_000,
  });
  return rows;
}

describe("SpendCard", () => {
  it("renders MTD total and metric breakdown from the summary", async () => {
    const client = makeStubClient({
      "/api/v1/spend/summary": SUMMARY,
    });
    renderWithProviders(<SpendCard provider="bandwidth" />, client);

    expect(await screen.findByText("$1.23")).toBeInTheDocument();
    expect(screen.getByText(/Spend this month/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Show breakdown" }));

    expect(screen.getByText("SMS out")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
  });

  // Contract addition: GET /spend/summary's optional `unrated_providers` flags a carrier
  // whose $0.00 cost means "no rate card resolved", not "genuinely free".
  it("shows a no-rate-card note and opens Rates via the callback for an unrated provider", async () => {
    const onOpenRates = vi.fn();
    const client = makeStubClient({
      "/api/v1/spend/summary": { ...SUMMARY, unrated_providers: ["bandwidth"] },
    });
    renderWithProviders(<SpendCard provider="bandwidth" onOpenRates={onOpenRates} />, client);

    expect(await screen.findByText(/No rate card — costs shown as \$0\.00/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "(set rates)" }));
    expect(onOpenRates).toHaveBeenCalledTimes(1);
  });

  it("does not show the no-rate-card note for a provider not in unrated_providers", async () => {
    const client = makeStubClient({
      "/api/v1/spend/summary": { ...SUMMARY, unrated_providers: ["bandwidth"] },
    });
    renderWithProviders(<SpendCard provider="telnyx" />, client);

    await screen.findByText(/Spend this month/);
    expect(screen.queryByText(/No rate card/)).not.toBeInTheDocument();
  });

  it("omits the note when unrated_providers is absent (backward-compatible response)", async () => {
    const client = makeStubClient({
      "/api/v1/spend/summary": SUMMARY,
    });
    renderWithProviders(<SpendCard provider="bandwidth" />, client);

    await screen.findByText(/Spend this month/);
    expect(screen.queryByText(/No rate card/)).not.toBeInTheDocument();
  });
});

describe("RatesDrawer", () => {
  it("PUTs only changed rows and converts 0.0040 dollars to 4000 micros", async () => {
    const client = makeStubClient({
      "/api/v1/provider-rates": ratesRoute,
    });
    renderWithProviders(<RatesDrawer readOnly={false} onClose={() => {}} />, client);

    const input = (await screen.findByLabelText("telnyx sms_out rate")) as HTMLInputElement;
    expect(input.value).toBe("0.0040");

    await userEvent.clear(input);
    await userEvent.type(input, "0.0050");
    await userEvent.click(screen.getByRole("button", { name: "Save rates" }));

    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/provider-rates" && c.init?.method === "PUT",
      );
      expect(call).toBeTruthy();
    });

    const call = client.calls.find(
      (c) => c.path === "/api/v1/provider-rates" && c.init?.method === "PUT",
    )!;
    const body = call.init?.json as {
      rates: Array<{ provider: string; metric: string; unit_cost_micros: number }>;
    };

    expect(body.rates).toHaveLength(1);
    expect(body.rates[0]).toMatchObject({
      provider: "telnyx",
      metric: "sms_out",
      unit_cost_micros: 5000,
    });
  });

  // P19 fix-required #1: Reset must use the backend-supplied default_unit_cost_micros, not
  // a frontend-hardcoded rate table (which could drift from the backend's real defaults).
  it("Reset restores an overridden row to its backend default_unit_cost_micros", async () => {
    const client = makeStubClient({
      "/api/v1/provider-rates": ratesRoute,
    });
    renderWithProviders(<RatesDrawer readOnly={false} onClose={() => {}} />, client);

    const input = (await screen.findByLabelText("telnyx number_mrc rate")) as HTMLInputElement;
    expect(input.value).toBe("0.3000");

    await userEvent.click(screen.getByRole("button", { name: "Reset telnyx number_mrc to default" }));
    expect(input.value).toBe("0.2500");

    await userEvent.click(screen.getByRole("button", { name: "Save rates" }));

    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === "/api/v1/provider-rates" && c.init?.method === "PUT",
      );
      expect(call).toBeTruthy();
    });

    const call = client.calls.find(
      (c) => c.path === "/api/v1/provider-rates" && c.init?.method === "PUT",
    )!;
    const body = call.init?.json as {
      rates: Array<{ provider: string; metric: string; unit_cost_micros: number }>;
    };

    expect(body.rates).toContainEqual({
      provider: "telnyx",
      metric: "number_mrc",
      unit_cost_micros: 250_000,
    });
  });

  it("disables inputs, Save, Reset, and Recalculate in read-only mode", async () => {
    const client = makeStubClient({
      "/api/v1/provider-rates": ratesRoute,
    });
    renderWithProviders(<RatesDrawer readOnly={true} onClose={() => {}} />, client);

    const input = (await screen.findByLabelText("telnyx sms_out rate")) as HTMLInputElement;
    expect(input).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save rates" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Reset telnyx sms_out to default" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "Recalculate today" })).toBeDisabled();
  });

  // P19 fix-required #6: a manual same-day rollup, separate from the hourly sweeper.
  it("Recalculate today POSTs a rollup for today's UTC day", async () => {
    const client = makeStubClient({
      "/api/v1/provider-rates": ratesRoute,
      "/api/v1/spend/rollup": { day: todayUTC() },
    });
    renderWithProviders(<RatesDrawer readOnly={false} onClose={() => {}} />, client);

    await screen.findByLabelText("telnyx sms_out rate");
    await userEvent.click(screen.getByRole("button", { name: "Recalculate today" }));

    await waitFor(() => {
      const call = client.calls.find(
        (c) => c.path === `/api/v1/spend/rollup?day=${todayUTC()}` && c.init?.method === "POST",
      );
      expect(call).toBeTruthy();
    });
  });

  // P19 fix-required #8: Recalculate's onSuccess invalidates the "spend" query prefix,
  // which refetches provider-rates in the background - an unsaved edit sitting in another
  // row must survive that refetch instead of being silently reseeded away.
  it("keeps an unsaved edit through a background refetch of the rates query", async () => {
    const client = makeStubClient({
      "/api/v1/provider-rates": ratesRoute,
      "/api/v1/spend/rollup": { day: todayUTC() },
    });
    renderWithProviders(<RatesDrawer readOnly={false} onClose={() => {}} />, client);

    const input = (await screen.findByLabelText("telnyx sms_out rate")) as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.type(input, "0.0099");
    expect(input.value).toBe("0.0099");

    await userEvent.click(screen.getByRole("button", { name: "Recalculate today" }));

    await waitFor(() => {
      expect(
        client.calls.some(
          (c) => c.path.startsWith("/api/v1/spend/rollup") && c.init?.method === "POST",
        ),
      ).toBe(true);
    });

    // The rollup invalidated ["spend"], which refetches provider-rates a second time.
    await waitFor(() => {
      const getCalls = client.calls.filter(
        (c) => c.path === "/api/v1/provider-rates" && !c.init?.method,
      );
      expect(getCalls.length).toBeGreaterThan(1);
    });

    expect(input.value).toBe("0.0099");
  });

  it("marks an invalid draft with aria-invalid and shows a hint, and blocks Save", async () => {
    const client = makeStubClient({
      "/api/v1/provider-rates": ratesRoute,
    });
    renderWithProviders(<RatesDrawer readOnly={false} onClose={() => {}} />, client);

    const input = (await screen.findByLabelText("telnyx sms_out rate")) as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.type(input, "-1");

    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Enter a rate ≥ 0")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save rates" })).toBeDisabled();
  });

  // P19 fix-required #11: the drawer is a real modal - aria-modal, Escape to close, and
  // focus moves in on open and back out on close.
  it("is a modal dialog: aria-modal is set, Escape closes it, and focus moves in and back out", async () => {
    const opener = document.createElement("button");
    opener.textContent = "Open rates";
    document.body.appendChild(opener);
    opener.focus();

    const onClose = vi.fn();
    const client = makeStubClient({
      "/api/v1/provider-rates": ratesRoute,
    });
    const view = renderWithProviders(<RatesDrawer readOnly={false} onClose={onClose} />, client);

    const dialog = await screen.findByRole("dialog", { name: "Provider rates" });
    expect(dialog).toHaveAttribute("aria-modal", "true");

    await waitFor(() => expect(screen.getByRole("button", { name: "Close" })).toHaveFocus());

    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);

    view.unmount();
    expect(opener).toHaveFocus();

    document.body.removeChild(opener);
  });
});

describe("SpendTile", () => {
  it("renders totals, per-provider list, and 30 daily bars", async () => {
    const client = makeStubClient({
      "/api/v1/spend/summary": SUMMARY,
      "/api/v1/spend/daily": dailyRows(),
    });
    renderWithProviders(<SpendTile />, client);

    expect(await screen.findByText("$12.00")).toBeInTheDocument();
    expect(screen.getByText("telnyx")).toBeInTheDocument();
    expect(screen.getByText("bandwidth")).toBeInTheDocument();

    expect(screen.getAllByRole("img", { name: /^Spend \d{4}-\d{2}-\d{2}: /})).toHaveLength(30);
  });

  // P19 fix-required #3: zero-fill every day BEFORE folding in returned rows - a day with
  // no spend row still renders its own (zero-height) bar.
  it("zero-fills days with no spend rows so exactly 30 bars always render", async () => {
    const client = makeStubClient({
      "/api/v1/spend/summary": SUMMARY,
      "/api/v1/spend/daily": gappedDailyRows(),
    });
    renderWithProviders(<SpendTile />, client);

    await screen.findByText("$12.00");

    const bars = screen.getAllByRole("img", { name: /^Spend \d{4}-\d{2}-\d{2}: /});
    expect(bars).toHaveLength(30);

    const zeroBar = bars.find((b) => b.getAttribute("aria-label")?.endsWith("$0.00"));
    expect(zeroBar).toBeTruthy();
    expect(zeroBar).toHaveStyle({ height: "0%" });
  });

  // P19 fix-required #3: the fold guards on `map.has(period_date)` - a row dated outside
  // the requested range must not add a 31st bar.
  it("ignores a spend row whose period_date falls outside the requested range", async () => {
    const client = makeStubClient({
      "/api/v1/spend/summary": SUMMARY,
      "/api/v1/spend/daily": dailyRowsWithOutOfRangeExtra(),
    });
    renderWithProviders(<SpendTile />, client);

    await screen.findByText("$12.00");
    expect(screen.getAllByRole("img", { name: /^Spend \d{4}-\d{2}-\d{2}: /})).toHaveLength(30);
  });

  // P19 fix-required #1: a daily-query failure must not read as "$0 every day" - the chart
  // block shows its own unavailable message while the MTD total/provider list stay visible.
  it("shows an unavailable message for the chart when the daily query errors, without hiding the total", async () => {
    const client = makeStubClient({
      "/api/v1/spend/summary": SUMMARY,
      "/api/v1/spend/daily": () => {
        throw new Error("daily spend down");
      },
    });
    renderWithProviders(<SpendTile />, client);

    expect(await screen.findByText("$12.00")).toBeInTheDocument();
    expect(await screen.findByText("Daily spend is unavailable.")).toBeInTheDocument();
    expect(screen.queryAllByRole("img", { name: /^Spend/ })).toHaveLength(0);
  });

  it('shows "No spend data for this range." when every day in the window is genuinely zero', async () => {
    const client = makeStubClient({
      "/api/v1/spend/summary": SUMMARY,
      "/api/v1/spend/daily": [],
    });
    renderWithProviders(<SpendTile />, client);

    expect(await screen.findByText("$12.00")).toBeInTheDocument();
    expect(await screen.findByText("No spend data for this range.")).toBeInTheDocument();
    expect(screen.queryAllByRole("img", { name: /^Spend/ })).toHaveLength(0);
  });

  // Contract addition: GET /spend/summary's optional `unrated_providers` - the tile shows
  // one summary line listing them (no drawer here; that's ProvidersPage's job).
  it("shows a line listing unrated providers when unrated_providers is non-empty", async () => {
    const client = makeStubClient({
      "/api/v1/spend/summary": { ...SUMMARY, unrated_providers: ["bandwidth", "telnyx"] },
      "/api/v1/spend/daily": dailyRows(),
    });
    renderWithProviders(<SpendTile />, client);

    expect(await screen.findByText("$12.00")).toBeInTheDocument();
    expect(screen.getByText("No rate card: bandwidth, telnyx")).toBeInTheDocument();
  });

  it("shows no unrated-providers line when the field is absent or empty", async () => {
    const client = makeStubClient({
      "/api/v1/spend/summary": SUMMARY,
      "/api/v1/spend/daily": dailyRows(),
    });
    renderWithProviders(<SpendTile />, client);

    await screen.findByText("$12.00");
    expect(screen.queryByText(/No rate card/)).not.toBeInTheDocument();
  });
});

describe("formatMicros", () => {
  it("formats micros as dollars", () => {
    expect(formatMicros(0)).toBe("$0.00");
    expect(formatMicros(1)).toBe("$0.00");
    expect(formatMicros(1_234_567)).toBe("$1.23");
  });

  // P19 fix-required #2 (corrected): the probe that actually bites a naive
  // .toFixed(2)-on-the-raw-dollar-float implementation is 1_005_000 micros (1.005
  // dollars), not 1_235_000 - `(1_005_000 / 1_000_000).toFixed(2)` gives "$1.00" because
  // 1.005 is stored as 1.00499999999999989... in IEEE754. Cent-level integer rounding
  // gets the correct "$1.01".
  it("rounds at the cent boundary instead of truncating the raw float", () => {
    expect(formatMicros(1_005_000)).toBe("$1.01");
  });

  it("groups thousands via Intl currency formatting", () => {
    expect(formatMicros(12_345_678_900)).toBe("$12,345.68");
  });

  it("formats a negative amount with a leading minus before the currency sign", () => {
    expect(formatMicros(-1_234_567)).toBe("-$1.23");
  });

  it("converts dollars to micros correctly", () => {
    expect(dollarsToMicros(0.004)).toBe(4000);
  });

  // P19 fix-required #2: Math.round on the float product, not a raw cast, so the classic
  // 1.005 / 8.61 float-imprecision cases still land on the exact integer micros.
  it("survives float imprecision in the dollars-to-micros conversion", () => {
    expect(dollarsToMicros(1.005)).toBe(1_005_000);
    expect(dollarsToMicros(8.61)).toBe(8_610_000);
  });
});
