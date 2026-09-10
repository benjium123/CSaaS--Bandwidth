import { describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PaymentMethods } from "./PaymentMethods";
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

const PAYMENT_METHODS = [
  {
    id: "pm-1",
    brand: "Visa",
    last4: "4242",
    is_default: true,
    exp_month: 12,
    exp_year: 2030,
  },
  {
    id: "pm-2",
    brand: "Mastercard",
    last4: "1111",
    is_default: false,
    exp_month: null,
    exp_year: null,
  },
];

function baseRoutes(overrides: Record<string, RouteStub | unknown> = {}) {
  return {
    "/api/v1/me/capabilities": CAPABILITIES,
    "/api/v1/billing/payment-methods": ((_path, init) => {
      if ((init.method ?? "GET") === "POST") {
        return { checkout_url: "https://checkout.example/card" };
      }
      if ((init.method ?? "GET") === "DELETE") {
        return undefined;
      }
      return PAYMENT_METHODS;
    }) as RouteStub,
    ...overrides,
  };
}

describe("PaymentMethods", () => {
  it("renders two cards with distinct accessible remove names", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<PaymentMethods />, client);

    expect(
      await screen.findByRole("button", { name: "Remove Visa ending 4242" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Remove Mastercard ending 1111" }),
    ).toBeInTheDocument();
  });

  it("renders the default badge", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<PaymentMethods />, client);

    expect(await screen.findByText("Default")).toBeInTheDocument();
  });

  it("renders the expiry line only when both fields are present", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<PaymentMethods />, client);

    const expiryLines = await screen.findAllByText(/^Expires /);
    expect(expiryLines).toHaveLength(1);
    expect(expiryLines[0]).toHaveTextContent("Expires 12/2030");
  });

  it("uses a two-step confirm before deleting a card", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<PaymentMethods />, client);

    await userEvent.click(
      await screen.findByRole("button", { name: "Remove Visa ending 4242" }),
    );
    expect(
      client.calls.some((call) => (call.init.method ?? "GET") === "DELETE"),
    ).toBe(false);

    await userEvent.click(
      screen.getByRole("button", { name: "Confirm removing Visa ending 4242" }),
    );

    await waitFor(() => {
      expect(
        client.calls.some(
          (call) =>
            call.path === "/api/v1/billing/payment-methods/pm-1" &&
            (call.init.method ?? "GET") === "DELETE",
        ),
      ).toBe(true);
    });
  });

  it("cancel returns to the remove state and sends nothing", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<PaymentMethods />, client);

    await userEvent.click(
      await screen.findByRole("button", { name: "Remove Visa ending 4242" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Cancel removing Visa ending 4242" }),
    );

    expect(
      screen.getByRole("button", { name: "Remove Visa ending 4242" }),
    ).toBeInTheDocument();
    expect(
      client.calls.some((call) => (call.init.method ?? "GET") === "DELETE"),
    ).toBe(false);
  });

  it("disables all controls without org billing permission", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/me/capabilities": {
          ...CAPABILITIES,
          permissions: ["settings:read"],
        },
      }),
    );
    renderWithProviders(<PaymentMethods />, client);

    expect(await screen.findByText("Only the workspace owner can change cards.")).toBeInTheDocument();
    // The sentence renders before the cards query resolves, so the buttons have to be
    // awaited separately - a getByRole here raced the fetch and found nothing.
    expect(await screen.findByRole("button", { name: "Add a card" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Remove Visa ending 4242" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Remove Mastercard ending 1111" })).toBeDisabled();
  });

  it("adds a card and calls the injected onCheckout", async () => {
    const onCheckout = vi.fn();
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<PaymentMethods onCheckout={onCheckout} />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Add a card" }));

    await waitFor(() => {
      const post = client.calls.find(
        (call) =>
          call.path === "/api/v1/billing/payment-methods" &&
          (call.init.method ?? "GET") === "POST",
      );
      expect(post).toBeDefined();
      expect(onCheckout).toHaveBeenCalledWith("https://checkout.example/card");
    });
  });

  it("shows a payment methods error with a retry that recovers", async () => {
    let methodsCalls = 0;
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/payment-methods": ((_path, init) => {
          if ((init.method ?? "GET") === "POST") {
            return { checkout_url: "https://checkout.example/card" };
          }
          methodsCalls += 1;
          if (methodsCalls === 1) return new Error("payments down");
          return PAYMENT_METHODS;
        }) as RouteStub,
      }),
    );
    renderWithProviders(<PaymentMethods />, client);

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("payments down")).toBeInTheDocument();

    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Visa ending 4242")).toBeInTheDocument();
  });
});
