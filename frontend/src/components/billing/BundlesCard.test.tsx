import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BundlesCard } from "./BundlesCard";
import { bundleQuote, type BundlesInfo } from "@/api/billing";
import { makeStubClient, renderWithProviders, type RouteStub } from "@/test/harness";

const CAPABILITIES = {
  permissions: ["settings:read", "org:billing"],
  org: {
    has_provider: true,
    has_number: true,
    member_count: 2,
    registration_state: "approved",
  },
};

const BUNDLES_PAYLOAD: BundlesInfo = {
  volume_min_qty: 5,
  volume_discount_bps: 2000,
  kinds: {
    sms: {
      units: 0,
      units_per_bundle: 1000,
      list_micros: 12_000_000,
      volume_discount: true,
      pay_as_you_go_micros: 15_000,
    },
    mms: {
      units: 0,
      units_per_bundle: 100,
      list_micros: 3_000_000,
      volume_discount: false,
      pay_as_you_go_micros: 35_000,
    },
  },
};

function baseRoutes(overrides: Record<string, RouteStub | unknown> = {}) {
  return {
    "/api/v1/me/capabilities": CAPABILITIES,
    "/api/v1/billing/bundles": BUNDLES_PAYLOAD,
    "/api/v1/billing/bundles/checkout": ((_path, _init) => ({
      checkout_url: "https://checkout.example/bundle",
      list_micros: 60_000_000,
      discount_micros: 12_000_000,
      paid_micros: 48_000_000,
      units: 5000,
    })) as RouteStub,
    ...overrides,
  };
}

/** Scopes queries to one kind's row, since both rows render a "Buy" button and a bundle-
 * quantity input with the same shape - only the row heading names the kind. */
function rowFor(label: string) {
  const heading = screen.getByText(label);
  return within(heading.closest("div") as HTMLElement);
}

describe("BundlesCard", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders units left and prices from the API payload", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<BundlesCard />, client);

    await screen.findByText("Text messages (SMS)");
    expect(screen.getAllByText("0 left")).toHaveLength(2);

    const smsRow = rowFor("Text messages (SMS)");
    expect(
      smsRow.getByText("1,000 texts for $12.00 — buy 5 or more at once and save 20%"),
    ).toBeInTheDocument();
    expect(smsRow.getByText("Without a bundle: $0.015 each")).toBeInTheDocument();

    const mmsRow = rowFor("Picture messages (MMS)");
    expect(mmsRow.getByText("100 picture messages for $3.00")).toBeInTheDocument();
    expect(mmsRow.getByText("Without a bundle: $0.035 each")).toBeInTheDocument();
  });

  it("shows no savings below the volume minimum and the savings line at or above it (SMS)", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<BundlesCard />, client);

    await screen.findByText("Text messages (SMS)");
    const smsRow = rowFor("Text messages (SMS)");
    const qtyInput = smsRow.getByLabelText("Text messages (SMS) bundle quantity");

    await userEvent.clear(qtyInput);
    await userEvent.type(qtyInput, "4");
    expect(smsRow.getByText("4 bundles · 4,000 texts · $48.00")).toBeInTheDocument();
    expect(smsRow.queryByText(/you save/)).not.toBeInTheDocument();
    expect(smsRow.getByText("Buy 5 or more to save 20%")).toBeInTheDocument();

    await userEvent.clear(qtyInput);
    await userEvent.type(qtyInput, "5");
    expect(
      smsRow.getByText("5 bundles · 5,000 texts · $48.00 (you save $12.00)"),
    ).toBeInTheDocument();
    expect(smsRow.queryByText("Buy 5 or more to save 20%")).not.toBeInTheDocument();
  });

  it("never shows savings for MMS, which has no volume discount", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<BundlesCard />, client);

    await screen.findByText("Picture messages (MMS)");
    const mmsRow = rowFor("Picture messages (MMS)");
    const qtyInput = mmsRow.getByLabelText("Picture messages (MMS) bundle quantity");

    await userEvent.clear(qtyInput);
    await userEvent.type(qtyInput, "5");
    expect(
      mmsRow.getByText("5 bundles · 500 picture messages · $15.00"),
    ).toBeInTheDocument();
    expect(mmsRow.queryByText(/you save/)).not.toBeInTheDocument();
  });

  it("posts the SMS checkout with the chosen quantity and redirects to checkout_url", async () => {
    const assign = vi.fn();
    const original = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...original, assign },
    });

    try {
      const client = makeStubClient(baseRoutes());
      renderWithProviders(<BundlesCard />, client);

      await screen.findByText("Text messages (SMS)");
      const smsRow = rowFor("Text messages (SMS)");
      const qtyInput = smsRow.getByLabelText("Text messages (SMS) bundle quantity");

      await userEvent.clear(qtyInput);
      await userEvent.type(qtyInput, "5");
      await userEvent.click(smsRow.getByRole("button", { name: "Buy" }));

      await waitFor(() => {
        const post = client.calls.find(
          (call) =>
            call.path === "/api/v1/billing/bundles/checkout" &&
            (call.init.method ?? "GET") === "POST",
        );
        expect(post).toBeDefined();
        expect(post?.init.json).toEqual({ kind: "sms", qty: 5 });
        expect(assign).toHaveBeenCalledWith("https://checkout.example/bundle");
      });
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: original });
    }
  });

  it("disables Buy without org billing permission", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/me/capabilities": { ...CAPABILITIES, permissions: ["settings:read"] },
      }),
    );
    renderWithProviders(<BundlesCard />, client);

    await screen.findByText("Text messages (SMS)");
    expect(screen.getByText("Only the workspace owner can buy bundles.")).toBeInTheDocument();
    const smsRow = rowFor("Text messages (SMS)");
    expect(smsRow.getByRole("button", { name: "Buy" })).toBeDisabled();
  });
});

describe("bundleQuote", () => {
  it("charges list price with no discount below the volume minimum", () => {
    const quote = bundleQuote(BUNDLES_PAYLOAD, "sms", 4);
    expect(quote).toEqual({
      list: 48_000_000,
      discount: 0,
      paid: 48_000_000,
      units: 4000,
      unitPaid: 12_000_000,
    });
  });

  it("applies the volume discount at and above the minimum qty", () => {
    const quote = bundleQuote(BUNDLES_PAYLOAD, "sms", 5);
    expect(quote).toEqual({
      list: 60_000_000,
      discount: 12_000_000,
      paid: 48_000_000,
      units: 5000,
      unitPaid: 9_600_000,
    });
  });

  it("never discounts a kind with volume_discount: false, regardless of qty", () => {
    const quote = bundleQuote(BUNDLES_PAYLOAD, "mms", 50);
    expect(quote).toEqual({
      list: 150_000_000,
      discount: 0,
      paid: 150_000_000,
      units: 5000,
      unitPaid: 3_000_000,
    });
  });
});
