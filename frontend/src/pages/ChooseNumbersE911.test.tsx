import { describe, it, expect } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { ChooseNumbersPage } from "./ChooseNumbersPage";

const me = { id: "u1", email: "admin@example.com", full_name: "Admin", permissions: ["numbers:manage"], memberships: [{ org_id: "org-1", role_name: "owner" }], is_platform_operator: true, operator_role: "admin" };

const numbers = [{ e164: "+12125550101", locality: "New York", region: "NY" }];

const savedAddress = { id: "addr-1", name: "HQ", street_address: "1 Main St", extended_address: null, locality: "Dallas", administrative_area: "TX", postal_code: "75201", country_code: "US", label: "1 Main St, Dallas, TX 75201" };

const catalog = [
  { code: "solo", name: "Solo", users: 1, numbers: 1, price_cents: 1500, minutes: 200, monthly_total_cents_if_switched: 1500 },
  { code: "team", name: "Team", users: 3, numbers: 3, price_cents: 4500, minutes: 600, monthly_total_cents_if_switched: 4500 },
  { code: "business", name: "Business", users: 5, numbers: 5, price_cents: 7500, minutes: 1000, monthly_total_cents_if_switched: 7500 },
];
const noPlan = { plan: null, users: { limit: null, in_use: 1 }, numbers: { limit: null, in_use: 0 }, extra_user_cents: 1500, extra_number_cents: 500, minutes_per_user: 200, catalog };

const baseRoutes = {
  "/api/v1/billing/plan": noPlan,
  "/api/v1/auth/me": me,
  "/api/v1/billing/number-purchases/current": null,
  "/api/v1/numbers/available?carrier=telnyx&area_code=212&limit=20": numbers,
  "/api/v1/numbers/emergency-addresses": { addresses: [], notice: "NOTICE-TEXT" },
  "/api/v1/billing/number-checkout": { id: "p1", state: "checkout", numbers: [] },
};

function routes(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { ...baseRoutes, ...overrides };
}

const ADDRESS_DEFAULTS = {
  "Business or person at this location": "Ada Studio",
  "Street address": "1 Main St",
  "Suite/floor/unit (optional)": "Suite 4",
  "City": "New York",
  "State (2-letter)": "TX",
  "ZIP code": "10001",
};

async function fillAddress(overrides: Record<string, string> = {}) {
  for (const [label, value] of Object.entries({ ...ADDRESS_DEFAULTS, ...overrides })) {
    if (value) await userEvent.type(screen.getByLabelText(label), value);
  }
}

async function pickNumber() {
  await userEvent.type(screen.getByLabelText("Area code"), "212");
  await userEvent.click(screen.getByRole("button", { name: "Search" }));
  await userEvent.click(await screen.findByRole("checkbox", { name: /\+12125550101/ }));
}

const ack = () => screen.getByRole("checkbox", { name: "I understand how 911 works with these numbers" });
const checkoutButton = () => screen.getByRole("button", { name: "Continue to payment" });
const checkoutBody = (client: ReturnType<typeof makeStubClient>) =>
  client.calls.find(c => c.path === "/api/v1/billing/number-checkout")?.init.json;

describe("E911 at number checkout", () => {
  it("new address: button stays disabled until the address is valid and the 911 box is ticked, then posts emergency_address", async () => {
    const client = makeStubClient(routes());
    renderWithProviders(<ChooseNumbersPage />, client);

    expect(await screen.findByText("NOTICE-TEXT")).toBeInTheDocument();
    await pickNumber();
    expect(checkoutButton()).toBeDisabled();

    // A bad state and a bad ZIP are each reported, and neither lets the button through.
    await fillAddress({ "State (2-letter)": "T1", "ZIP code": "123" });
    expect(screen.getByText("Use the 2-letter state, e.g. TX.")).toBeInTheDocument();
    expect(screen.getByText("Enter a 5-digit ZIP code.")).toBeInTheDocument();
    await userEvent.click(ack());
    expect(checkoutButton()).toBeDisabled();

    // Fixing the ZIP clears its problem; the still-unticked box keeps the button blocked.
    await userEvent.clear(screen.getByLabelText("ZIP code"));
    await userEvent.type(screen.getByLabelText("ZIP code"), "10001");
    expect(screen.queryByText("Enter a 5-digit ZIP code.")).not.toBeInTheDocument();
    await userEvent.click(ack());
    await userEvent.clear(screen.getByLabelText("State (2-letter)"));
    await userEvent.type(screen.getByLabelText("State (2-letter)"), "tx");
    expect(checkoutButton()).toBeDisabled();

    await userEvent.click(ack());
    expect(checkoutButton()).toBeEnabled();
    await userEvent.click(checkoutButton());

    await waitFor(() => expect(checkoutBody(client)).toEqual({
      plan_code: "solo",
      billing_interval: "month",
      numbers: ["+12125550101"],
      acknowledge_e911: true,
      emergency_address: {
        name: "Ada Studio",
        street_address: "1 Main St",
        extended_address: "Suite 4",
        locality: "New York",
        administrative_area: "TX",
        postal_code: "10001",
        country_code: "US",
      },
    }));
  });

  it("blank optional unit is omitted", async () => {
    const client = makeStubClient(routes());
    renderWithProviders(<ChooseNumbersPage />, client);

    await pickNumber();
    await fillAddress({ "Suite/floor/unit (optional)": "" });
    await userEvent.click(ack());
    await userEvent.click(checkoutButton());

    await waitFor(() => {
      const body = checkoutBody(client) as { emergency_address?: Record<string, unknown> };
      expect(body.emergency_address).toEqual({
        name: "Ada Studio",
        street_address: "1 Main St",
        locality: "New York",
        administrative_area: "TX",
        postal_code: "10001",
        country_code: "US",
      });
    });
  });

  it("saved address: picking one sends emergency_address_id and no address object", async () => {
    const client = makeStubClient(routes({ "/api/v1/numbers/emergency-addresses": { addresses: [savedAddress], notice: "NOTICE-TEXT" } }));
    renderWithProviders(<ChooseNumbersPage />, client);

    await pickNumber();
    // The saved list has loaded (so the absence below is real, not a loading state).
    await screen.findByRole("radio", { name: /1 Main St, Dallas, TX 75201/ });
    expect(screen.queryByLabelText("Street address")).not.toBeInTheDocument();
    expect(checkoutButton()).toBeDisabled();

    await userEvent.click(ack());
    expect(checkoutButton()).toBeDisabled();

    await userEvent.click(screen.getByRole("radio", { name: /1 Main St, Dallas, TX 75201/ }));
    expect(checkoutButton()).toBeEnabled();
    await userEvent.click(checkoutButton());

    await waitFor(() => expect(checkoutBody(client)).toEqual({
      plan_code: "solo",
      billing_interval: "month",
      numbers: ["+12125550101"],
      acknowledge_e911: true,
      emergency_address_id: "addr-1",
    }));
  });

  it("A different address reveals the form", async () => {
    const client = makeStubClient(routes({ "/api/v1/numbers/emergency-addresses": { addresses: [savedAddress], notice: "NOTICE-TEXT" } }));
    renderWithProviders(<ChooseNumbersPage />, client);

    await pickNumber();
    // The saved list has loaded (so the absence below is real, not a loading state).
    await screen.findByRole("radio", { name: /1 Main St, Dallas, TX 75201/ });
    expect(screen.queryByLabelText("Street address")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("radio", { name: "A different address" }));
    expect(screen.getByLabelText("Street address")).toBeInTheDocument();

    await userEvent.click(ack());
    expect(checkoutButton()).toBeDisabled();
    await fillAddress();
    expect(checkoutButton()).toBeEnabled();
  });

  it("shows the carrier's validation message verbatim", async () => {
    const client = makeStubClient(routes({
      "/api/v1/billing/number-checkout": () => new Error("Address could not be validated. Did you mean: 1 Main Street, Dallas, TX 75201?"),
    }));
    renderWithProviders(<ChooseNumbersPage />, client);

    await pickNumber();
    await fillAddress();
    await userEvent.click(ack());
    await userEvent.click(checkoutButton());

    expect(await screen.findByRole("alert")).toHaveTextContent(/^Address could not be validated\. Did you mean: 1 Main Street, Dallas, TX 75201\?$/);
  });

  it("a failed address lookup keeps payment disabled", async () => {
    const client = makeStubClient(routes({ "/api/v1/numbers/emergency-addresses": () => new Error("boom") }));
    renderWithProviders(<ChooseNumbersPage />, client);

    await pickNumber();
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    expect(checkoutButton()).toBeDisabled();
  });
});
