import { describe, expect, it, vi } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NumbersPage } from "./NumbersPage";
import { pendingRefetchInterval, type NumberOut } from "@/api/numbers";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const CATALOG = [
  {
    name: "bandwidth",
    live: true,
    enabled_flag: true,
    primary: true,
    supports_voice: true,
    supports_messaging: true,
    supports_numbers: true,
    missing: [],
    reason: "",
    state: "closed",
    capabilities: { max_media_bytes: 0 },
  },
  {
    name: "telnyx",
    live: true,
    enabled_flag: true,
    primary: false,
    supports_voice: true,
    supports_messaging: true,
    supports_numbers: true,
    missing: [],
    reason: "",
    state: "closed",
    capabilities: { max_media_bytes: 1600 },
  },
  {
    name: "twilio",
    live: false,
    enabled_flag: false,
    primary: false,
    supports_voice: true,
    supports_messaging: true,
    supports_numbers: true,
    missing: ["auth_token"],
    reason: "Missing auth_token",
    state: "closed",
    capabilities: { max_media_bytes: 1600 },
  },
  {
    name: "plivo",
    live: false,
    enabled_flag: false,
    primary: false,
    supports_voice: true,
    supports_messaging: true,
    supports_numbers: true,
    missing: ["auth_token"],
    reason: "Missing auth_token",
    state: "closed",
    capabilities: { max_media_bytes: 0 },
  },
  {
    name: "signalwire",
    live: true,
    enabled_flag: true,
    primary: false,
    supports_voice: true,
    supports_messaging: true,
    supports_numbers: true,
    missing: [],
    reason: "",
    state: "closed",
    capabilities: { max_media_bytes: 1600 },
  },
];

const PROVIDER_ACCOUNTS = [
  {
    id: "pa-telnyx",
    provider: "telnyx",
    label: "Prod Telnyx",
    status: "active",
    last_probe_at: null,
    last_probe_detail: null,
    credentials: {},
  },
];

function numberFixture(overrides: Record<string, unknown> = {}) {
  return {
    id: "num-1",
    e164: "+12145550100",
    carrier: "bandwidth",
    provider_account_id: null,
    provider_account_label: null,
    purchase_cost_cents: null,
    monthly_cost_cents: 150,
    purchased_at: "2025-01-01T00:00:00Z",
    order_detail: null,
    is_active: true,
    number_type: "local",
    status: "active",
    capabilities: {},
    campaign_id: null,
    registration: "approved",
    registration_detail: "10DLC campaign verified",
    ...overrides,
  };
}

const AVAILABLE = [
  {
    e164: "+12145550111",
    number_type: "local",
    region: "TX",
    locality: "Dallas",
    // monthly_cost stays the generated non-null string even when cents are present -
    // formatMonthlyCost() just prefers the cents field when both are set.
    monthly_cost: "12.00",
    monthly_cost_cents: 1200,
    setup_cost_cents: 400,
    capabilities: {},
  },
];

const AVAILABLE_WITH_FALLBACK = [
  ...AVAILABLE,
  {
    e164: "+12145550112",
    number_type: "local",
    region: "TX",
    locality: "Austin",
    monthly_cost: "0.75",
    monthly_cost_cents: null,
    setup_cost_cents: null,
    capabilities: {},
  },
];

const ORDERED_NUMBER = numberFixture({
  id: "num-2",
  e164: "+12145550111",
  status: "pending",
  registration: "unknown",
  registration_detail: "",
});

/**
 * NOTE on key order: test/harness.tsx's stub client resolves a request by
 * `Object.keys(routes).find((k) => path.startsWith(k))`, i.e. the FIRST key (in
 * insertion order) the request path starts with wins - it is not longest-match.
 * "/api/v1/numbers" is a string-prefix of "/api/v1/numbers/available",
 * "/api/v1/numbers/order", "/api/v1/numbers/num-1", and
 * "/api/v1/numbers/num-1/campaign", so every one of those more specific routes MUST be
 * declared before the bare "/api/v1/numbers" key below, or the general list stub
 * silently shadows them. Pre-declaring them here (even with placeholder values) also
 * protects `baseStubs({...overrides})`: spreading an override for a key that already
 * exists updates its value in place without moving its position, so overrides stay
 * ordered correctly too - a NEW key added only via an override would still land after
 * "/api/v1/numbers" and be shadowed.
 */
function baseStubs(overrides: Record<string, unknown> = {}) {
  return {
    "/api/v1/numbers/available": [],
    "/api/v1/numbers/order": null,
    "/api/v1/numbers/num-1/campaign": numberFixture(),
    "/api/v1/numbers/num-1": numberFixture(),
    "/api/v1/registration/campaigns": [],
    "/api/v1/routing/catalog": CATALOG,
    "/api/v1/provider-accounts": PROVIDER_ACCOUNTS,
    "/api/v1/numbers": [numberFixture()],
    ...overrides,
  };
}

describe("NumbersPage", () => {
  it("renders the live carrier union and disables providers that are not live", async () => {
    const client = makeStubClient(baseStubs());
    renderWithProviders(<NumbersPage />, client);

    // Telnyx is live via both the env catalog AND a DB-backed account - the account
    // label wins.
    expect(
      await screen.findByRole("option", { name: "Telnyx (account: Prod Telnyx)" }),
    ).toBeInTheDocument();

    // Env-live, no DB account.
    expect(screen.getByRole("option", { name: "Bandwidth (env)" })).not.toBeDisabled();
    expect(screen.getByRole("option", { name: "SignalWire (env)" })).not.toBeDisabled();

    // Not live anywhere - disabled, tooltip carries the catalog's reason.
    const twilio = screen.getByRole("option", { name: "Twilio" });
    expect(twilio).toBeDisabled();
    expect(twilio).toHaveAttribute("title", "Missing auth_token");

    const plivo = screen.getByRole("option", { name: "Plivo" });
    expect(plivo).toBeDisabled();
    expect(plivo).toHaveAttribute("title", "Missing auth_token");
  });

  it("formats costs from cents and falls back to the legacy monthly_cost string", async () => {
    const client = makeStubClient(
      baseStubs({ "/api/v1/numbers/available": AVAILABLE_WITH_FALLBACK }),
    );
    renderWithProviders(<NumbersPage />, client);

    // List row: monthly_cost_cents = 150 -> "$1.50".
    expect(await screen.findByText("$1.50")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    // Search result 1: cents present -> formatted from cents.
    expect(await screen.findByText("$12.00")).toBeInTheDocument();
    expect(screen.getByText("$4.00")).toBeInTheDocument();
    // Search result 2: no cents -> falls back to the raw legacy string, unformatted.
    expect(screen.getByText("0.75")).toBeInTheDocument();
  });

  it("shows a pending badge and only enables the list refetch interval while a number is pending", async () => {
    const client = makeStubClient(
      baseStubs({ "/api/v1/numbers": [numberFixture({ status: "pending" })] }),
    );
    renderWithProviders(<NumbersPage />, client);

    expect(await screen.findByText("Pending")).toBeInTheDocument();

    expect(pendingRefetchInterval([numberFixture({ status: "pending" }) as NumberOut])).toBe(
      15000,
    );
    expect(pendingRefetchInterval([numberFixture({ status: "active" }) as NumberOut])).toBe(
      false,
    );
    expect(pendingRefetchInterval(undefined)).toBe(false);
  });

  it("shows the grant inbox link after ordering a number", async () => {
    const client = makeStubClient(
      baseStubs({
        "/api/v1/numbers/available": AVAILABLE,
        "/api/v1/numbers/order": ORDERED_NUMBER,
      }),
    );
    renderWithProviders(<NumbersPage />, client);

    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    const orderButton = await screen.findByRole("button", { name: "Order" });
    await userEvent.click(orderButton);

    const link = await screen.findByRole("link", {
      name: /Grant this inbox to a department or employee/,
    });
    expect(link).toHaveAttribute("href", "/settings/inboxes");
    expect(screen.getByText(/Ordered \+12145550111 \(pending\)/)).toBeInTheDocument();
  });

  it("orders using the carrier the results were searched with, not whatever the live dropdown shows now", async () => {
    const client = makeStubClient(
      baseStubs({
        "/api/v1/numbers/available": AVAILABLE,
        "/api/v1/numbers/order": ORDERED_NUMBER,
      }),
    );
    renderWithProviders(<NumbersPage />, client);

    const carrierSelect = await screen.findByLabelText("Carrier");
    await userEvent.selectOptions(carrierSelect, "bandwidth");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    // Change the dropdown AFTER the search fired - the order must still use the carrier
    // the results were actually searched/shown with (bandwidth), never the dropdown's
    // current live value (telnyx).
    await userEvent.selectOptions(carrierSelect, "telnyx");

    const orderButton = await screen.findByRole("button", { name: "Order" });
    await userEvent.click(orderButton);

    await waitFor(() => {
      const orderCall = client.calls.find((c) => c.path === "/api/v1/numbers/order");
      expect(orderCall?.init.json).toEqual({
        e164: "+12145550111",
        carrier: "bandwidth",
        monthly_cost_cents: 1200,
        setup_cost_cents: 400,
      });
    });
  });

  it("omits monthly_cost_cents/setup_cost_cents from the order body when the row has null cents", async () => {
    const nullCostRow = {
      e164: "+12145550113",
      number_type: "local",
      region: "TX",
      locality: "Fort Worth",
      monthly_cost: "0.50",
      monthly_cost_cents: null,
      setup_cost_cents: null,
      capabilities: {},
    };
    const client = makeStubClient(
      baseStubs({
        "/api/v1/numbers/available": [nullCostRow],
        "/api/v1/numbers/order": numberFixture({
          id: "num-3",
          e164: nullCostRow.e164,
          status: "pending",
        }),
      }),
    );
    renderWithProviders(<NumbersPage />, client);

    const carrierSelect = await screen.findByLabelText("Carrier");
    await userEvent.selectOptions(carrierSelect, "bandwidth");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    const orderButton = await screen.findByRole("button", { name: "Order" });
    await userEvent.click(orderButton);

    await waitFor(() => {
      const orderCall = client.calls.find((c) => c.path === "/api/v1/numbers/order");
      expect(orderCall?.init.json).toEqual({ e164: nullCostRow.e164, carrier: "bandwidth" });
      expect(orderCall?.init.json).not.toHaveProperty("monthly_cost_cents");
      expect(orderCall?.init.json).not.toHaveProperty("setup_cost_cents");
    });
  });

  it("polls the list every 15s while a number is pending", async () => {
    vi.useFakeTimers();
    try {
      const client = makeStubClient(
        baseStubs({ "/api/v1/numbers": [numberFixture({ status: "pending" })] }),
      );
      renderWithProviders(<NumbersPage />, client);

      await vi.waitFor(() =>
        expect(client.calls.filter((c) => c.path === "/api/v1/numbers").length).toBe(1),
      );

      await act(() => vi.advanceTimersByTimeAsync(15000));

      await vi.waitFor(() =>
        expect(
          client.calls.filter((c) => c.path === "/api/v1/numbers").length,
        ).toBeGreaterThanOrEqual(2),
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not poll the list once every number is active", async () => {
    vi.useFakeTimers();
    try {
      const client = makeStubClient(baseStubs());
      renderWithProviders(<NumbersPage />, client);

      await vi.waitFor(() =>
        expect(client.calls.filter((c) => c.path === "/api/v1/numbers").length).toBe(1),
      );

      await act(() => vi.advanceTimersByTimeAsync(20000));

      expect(client.calls.filter((c) => c.path === "/api/v1/numbers").length).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("surfaces a search failure with an alert instead of the generic 'no numbers found' text", async () => {
    const client = makeStubClient(
      baseStubs({ "/api/v1/numbers/available": new Error("carrier not configured") }),
    );
    renderWithProviders(<NumbersPage />, client);

    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    // role="alert" doesn't support "name from content" per the ARIA spec, so match by
    // role alone and assert the text separately.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("carrier not configured");
    expect(screen.queryByText("No numbers found.")).not.toBeInTheDocument();
  });

  it("shows the order_detail for a failed number", async () => {
    const client = makeStubClient(
      baseStubs({
        "/api/v1/numbers": [
          numberFixture({
            status: "failed",
            order_detail: "Bandwidth order failed: no inventory",
          }),
        ],
      }),
    );
    renderWithProviders(<NumbersPage />, client);

    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("Bandwidth order failed: no inventory")).toBeInTheDocument();
  });

  it("still requires release confirm before calling DELETE", async () => {
    const client = makeStubClient(
      baseStubs({ "/api/v1/numbers/num-1": numberFixture({ status: "released" }) }),
    );
    renderWithProviders(<NumbersPage />, client);

    await screen.findByText("(214) 555-0100");
    await userEvent.click(screen.getByRole("button", { name: "Release" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm release" }));

    await waitFor(() => {
      expect(
        client.calls.some(
          (c) => c.path === "/api/v1/numbers/num-1" && c.init.method === "DELETE",
        ),
      ).toBe(true);
    });
  });

  it("still assigns a campaign to a local number", async () => {
    const client = makeStubClient(
      baseStubs({
        "/api/v1/registration/campaigns": [{ id: "camp-1", name: "10DLC Main" }],
      }),
    );
    renderWithProviders(<NumbersPage />, client);

    const select = await screen.findByLabelText("Campaign for +12145550100");
    await userEvent.selectOptions(select, "camp-1");

    await waitFor(() => {
      const call = client.calls.find((c) => c.path === "/api/v1/numbers/num-1/campaign");
      expect(call?.init.method).toBe("PATCH");
      expect(call?.init.json).toEqual({ campaign_id: "camp-1" });
    });
  });
});
