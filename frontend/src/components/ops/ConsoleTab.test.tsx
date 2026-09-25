import { describe, it, expect } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConsoleTab } from "./ConsoleTab";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const ORG_A = {
  org_id: "org-a",
  name: "Alpha Realty",
  slug: "alpha-realty",
  created_at: "2026-01-01T00:00:00Z",
  prepaid: false,
  billing_state: "ok",
  balance_micros: 50_000_000,
  warn_threshold_micros: 5_000_000,
  avg_daily_spend_micros: 100_000,
  auto_recharge: true,
  auto_recharge_failures: 0,
  sms_bundle_units: 100,
  mms_bundle_units: 10,
  numbers: 2,
  plan_code: "growth",
  metrics: {
    paid: 20_000_000,
    list: 22_000_000,
    discount: 2_000_000,
    stripe_fees: 500_000,
    usage_revenue: 8_000_000,
    carrier_cost: 3_000_000,
    cash_profit: 16_500_000,
    sms_out_segments: 500,
    sms_in_segments: 300,
    minutes_out: 40,
    minutes_in: 20,
    blocked_total: 5,
    blocked_credit: 2,
    blocked_compliance: 2,
    blocked_moderation: 1,
    calls_out: 10,
    calls_in: 4,
    calls_out_answered: 8,
    calls_in_answered: 3,
    mms_out: 6,
    mms_in: 2,
    carrier_telnyx_texts: 800,
    carrier_telnyx_calls: 14,
  },
};

const ORG_B = {
  ...ORG_A,
  org_id: "org-b",
  name: "Bravo Homes",
  slug: "bravo-homes",
  billing_state: "low",
  balance_micros: 5_000_000,
  metrics: {
    ...ORG_A.metrics,
    paid: 60_000_000,
    cash_profit: 45_000_000,
  },
};

const ORGS_RESPONSE = {
  start: "2026-08-26",
  end: "2026-09-25",
  orgs: [ORG_A, ORG_B],
  totals: {
    paid: 80_000_000,
    list: 84_000_000,
    discount: 4_000_000,
    stripe_fees: 1_000_000,
    usage_revenue: 16_000_000,
    carrier_cost: 6_000_000,
    cash_profit: 61_500_000,
    sms_out_segments: 1000,
    sms_in_segments: 600,
    mms_out: 12,
    mms_in: 4,
    calls_out: 20,
    calls_in: 8,
    calls_out_answered: 16,
    calls_in_answered: 6,
    minutes_out: 80,
    minutes_in: 40,
    blocked_total: 10,
    blocked_credit: 4,
    blocked_compliance: 4,
    blocked_moderation: 2,
    carrier_telnyx_texts: 1600,
    carrier_telnyx_calls: 28,
  },
  summary: {
    orgs: 2,
    prepaid_orgs: 0,
    low_orgs: 1,
    exhausted_orgs: 0,
    balances_micros: 55_000_000,
    active_numbers: 4,
    sms_bundle_units_outstanding: 200,
    mms_bundle_units_outstanding: 20,
  },
};

const ORG_A_DETAIL = {
  org: {
    org_id: "org-a",
    name: "Alpha Realty",
    slug: "alpha-realty",
    prepaid: false,
    billing_state: "ok",
    balance_micros: 50_000_000,
    warn_threshold_micros: 5_000_000,
    avg_daily_spend_micros: 100_000,
    auto_recharge: { enabled: true },
    auto_recharge_failures: 0,
    sms_bundle_units: 100,
    mms_bundle_units: 10,
    telnyx_billing_group_id: null,
  },
  start: "2026-08-26",
  end: "2026-09-25",
  metrics: ORG_A.metrics,
  series: [
    {
      date: "2026-09-24",
      sms_out_segments: 10,
      sms_in_segments: 5,
      mms_out: 1,
      mms_in: 0,
      calls_out: 2,
      calls_in: 1,
      minutes_out: 3,
      minutes_in: 1,
      usage_revenue: 100_000,
      paid: 0,
      blocked_total: 0,
    },
  ],
  ledger: [
    {
      seq: 1,
      type: "adjustment",
      amount_micros: 1_000_000,
      balance_after_micros: 50_000_000,
      reference: "console:x",
      note: "launch credit",
      at: "2026-09-20T00:00:00Z",
    },
  ],
  payments: [],
  refusals: [],
  numbers: [],
};

function setup() {
  const client = makeStubClient({
    "/api/v1/auth/me": {
      id: "admin-1",
      email: "admin@example.com",
      is_platform_operator: true,
      operator_role: "admin",
      memberships: [],
    },
    "/api/v1/ops/console/orgs.csv": new Blob(["csv"]),
    "/api/v1/ops/console/orgs/org-a": ORG_A_DETAIL,
    "/api/v1/ops/console/orgs/org-a/adjust": { balance_after_micros: 62_500_000 },
    "/api/v1/ops/console/orgs/org-a/bundles": { units_after: 150 },
    "/api/v1/ops/console/orgs/org-a/prepaid": { prepaid: true },
    "/api/v1/ops/console/orgs": ORGS_RESPONSE,
    "/api/v1/ops/console/payments": { payments: [] },
    "/api/v1/ops/console/prices": {
      prices: [
        {
          metric: "sms_out",
          price_micros: 7500,
          default_micros: 7500,
          note: null,
          updated_at: null,
        },
        {
          metric: "number_mrc",
          price_micros: 1_000_000,
          default_micros: 1_000_000,
          note: null,
          updated_at: null,
        },
      ],
    },
  });
  renderWithProviders(<ConsoleTab />, client);
  return client;
}

describe("ConsoleTab", () => {
  it("renders KPI numbers from the fixture", async () => {
    setup();
    expect(await screen.findByText("$80.00")).toBeInTheDocument(); // Paid total
    expect(screen.getByText("$61.50")).toBeInTheDocument(); // Cash profit total
    expect(screen.getByText("$55.00")).toBeInTheDocument(); // Balances held
  });

  it("sorts the orgs table by a column", async () => {
    setup();
    // Scope to the workspaces table specifically - the traffic section above it has its
    // own <table> (per-carrier counts) whose rows would otherwise shift the indices.
    const orgsTable = (await screen.findByText("Alpha Realty")).closest("table")!;
    // Header row + Alpha then Bravo (fixture order).
    let rows = within(orgsTable).getAllByRole("row");
    expect(within(rows[1]).getByText("Alpha Realty")).toBeInTheDocument();

    await userEvent.click(within(orgsTable).getByRole("button", { name: /^Cash profit/ }));
    rows = within(orgsTable).getAllByRole("row");
    expect(within(rows[1]).getByText("Alpha Realty")).toBeInTheDocument();

    await userEvent.click(within(orgsTable).getByRole("button", { name: /^Cash profit/ }));
    rows = within(orgsTable).getAllByRole("row");
    expect(within(rows[1]).getByText("Bravo Homes")).toBeInTheDocument();
  });

  it("opens an org drawer with its detail", async () => {
    setup();
    await userEvent.click(await screen.findByRole("button", { name: "Alpha Realty" }));
    expect(await screen.findByRole("dialog", { name: "Alpha Realty" })).toBeInTheDocument();
    expect(await screen.findByText(/launch credit/)).toBeInTheDocument();
  });

  it("submits a credit adjustment with the correct micros", async () => {
    const client = setup();
    await userEvent.click(await screen.findByRole("button", { name: "Alpha Realty" }));
    await screen.findByRole("dialog", { name: "Alpha Realty" });

    await userEvent.type(screen.getByLabelText("Adjustment amount in dollars"), "12.50");
    await userEvent.type(screen.getByLabelText("Adjustment note"), "Launch credit for pilot");
    await userEvent.click(screen.getByRole("button", { name: "Apply adjustment" }));

    await waitFor(() =>
      expect(
        client.calls.find((c) => c.path === "/api/v1/ops/console/orgs/org-a/adjust")?.init.json,
      ).toMatchObject({ amount_micros: 12_500_000, note: "Launch credit for pilot" }),
    );
  });

  it("edits a price and PUTs the correct micros", async () => {
    const client = setup();
    const smsRow = (await screen.findByText("sms_out")).closest("tr")!;
    await userEvent.click(within(smsRow).getByRole("button", { name: "Edit" }));
    const input = within(smsRow).getByLabelText("Price for sms_out in dollars");
    await userEvent.clear(input);
    await userEvent.type(input, "0.0095");
    await userEvent.click(within(smsRow).getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(
        client.calls.find((c) => c.path === "/api/v1/ops/console/prices/sms_out")?.init.json,
      ).toMatchObject({ price_micros: 9500 }),
    );
  });
});
