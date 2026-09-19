import { describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BalanceCard } from "./BalanceCard";
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
  },
];

function makeSummary(overrides: Record<string, unknown> = {}) {
  const base = {
    balance_micros: 12_345_678,
    reserved_micros: 0,
    warning: null,
    auto_recharge: null,
    last_topup: null,
    ...overrides,
  } as Record<string, unknown> & { balance_micros: number; reserved_micros: number };
  // available_micros mirrors the server's own max(balance - reserved, 0) unless a test
  // states it outright, so a fixture cannot drift into a shape the server never sends.
  return {
    available_micros: Math.max(base.balance_micros - base.reserved_micros, 0),
    ...base,
  };
}

function baseRoutes(overrides: Record<string, RouteStub | unknown> = {}) {
  return {
    "/api/v1/me/capabilities": CAPABILITIES,
    "/api/v1/billing/summary": makeSummary(),
    "/api/v1/billing/payment-methods": PAYMENT_METHODS,
    "/api/v1/billing/topups": ((_path, _init) => ({
      checkout_url: "https://checkout.example/pay",
    })) as RouteStub,
    "/api/v1/billing/auto-recharge": ((_path, _init) => ({
      success: true,
    })) as RouteStub,
    ...overrides,
  };
}

describe("BalanceCard", () => {
  it("renders the balance in dollars, not micros", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/summary": makeSummary({ balance_micros: 12_345_678 }),
      }),
    );
    renderWithProviders(<BalanceCard />, client);

    expect(await screen.findByText("$12.35")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("12345678");
  });

  it("shows the on-hold line only when reserved_micros is greater than 0", async () => {
    const noReserveClient = makeStubClient(
      baseRoutes({ "/api/v1/billing/summary": makeSummary({ reserved_micros: 0 }) }),
    );
    const first = renderWithProviders(<BalanceCard />, noReserveClient);
    await screen.findByText("$12.35");
    expect(screen.queryByText(/is on hold for calls in progress/)).not.toBeInTheDocument();
    first.unmount();

    const reserveClient = makeStubClient(
      baseRoutes({ "/api/v1/billing/summary": makeSummary({ reserved_micros: 1_000_000 }) }),
    );
    renderWithProviders(<BalanceCard />, reserveClient);
    expect(
      await screen.findByText("$1.00 of your $12.35 balance is on hold for calls in progress."),
    ).toBeInTheDocument();
  });

  // The regression: the card used to headline balance_micros, which includes money already
  // reserved for calls in flight, so it told the customer they could spend $50 while $20 of
  // it was committed. The headline must be the server's available_micros.
  it("headlines the available credit, not the raw balance, while a call holds a reserve", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/summary": makeSummary({
          balance_micros: 50_000_000,
          reserved_micros: 20_000_000,
          available_micros: 30_000_000,
        }),
      }),
    );
    renderWithProviders(<BalanceCard />, client);

    const headline = await screen.findByText("$30.00");
    expect(headline.className).toContain("text-3xl");
    // The raw balance may appear only inside the sentence that explains the hold. Every
    // other "$50.00" on the card is the top-up preset button of that amount.
    screen
      .getAllByText("$50.00")
      .forEach((element) => expect(element.closest("button")).not.toBeNull());
    expect(
      screen.getByText("$20.00 of your $50.00 balance is on hold for calls in progress."),
    ).toBeInTheDocument();
  });

  // The pair for the test above: with nothing reserved the headline IS the balance, which
  // proves the previous test passes because the reserve was subtracted and not because the
  // headline happened to move.
  it("headlines the full balance when nothing is reserved", async () => {
    const client = makeStubClient(
      baseRoutes({
        // $60 rather than $50 only because "$50.00" is also a top-up preset button, and
        // the headline has to be findable unambiguously.
        "/api/v1/billing/summary": makeSummary({
          balance_micros: 60_000_000,
          reserved_micros: 0,
          available_micros: 60_000_000,
        }),
      }),
    );
    renderWithProviders(<BalanceCard />, client);

    const headline = await screen.findByText("$60.00");
    expect(headline.className).toContain("text-3xl");
    expect(screen.queryByText(/is on hold for calls in progress/)).not.toBeInTheDocument();
  });

  it("posts a preset top-up and calls the injected checkout redirect", async () => {
    const onCheckout = vi.fn();
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<BalanceCard onCheckout={onCheckout} />, client);

    await screen.findByText("$12.35");
    await userEvent.click(screen.getByRole("button", { name: "$25.00" }));

    await waitFor(() => {
      const post = client.calls.find(
        (call) =>
          call.path === "/api/v1/billing/topups" && (call.init.method ?? "GET") === "POST",
      );
      expect(post).toBeDefined();
      expect(post?.init.json).toEqual({ amount_micros: 25_000_000 });
      expect(onCheckout).toHaveBeenCalledWith("https://checkout.example/pay");
    });
  });

  it("posts a custom dollar amount as micros", async () => {
    const client = makeStubClient(baseRoutes());
    // onCheckout is injected even where the redirect is not what is being asserted:
    // the default calls window.location.assign, which jsdom logs as an unimplemented
    // navigation error on every successful top-up.
    renderWithProviders(<BalanceCard onCheckout={vi.fn()} />, client);

    await screen.findByText("$12.35");
    await userEvent.type(screen.getByLabelText("Other amount in dollars"), "37.50");
    await userEvent.click(screen.getByRole("button", { name: "Add credits" }));

    await waitFor(() => {
      const post = client.calls.find(
        (call) =>
          call.path === "/api/v1/billing/topups" && (call.init.method ?? "GET") === "POST",
      );
      expect(post).toBeDefined();
      expect(post?.init.json).toEqual({ amount_micros: 37_500_000 });
    });
  });

  it("shows the plain sentence for junk custom amounts and sends no top-up request", async () => {
    const client = makeStubClient(baseRoutes());
    renderWithProviders(<BalanceCard />, client);

    await screen.findByText("$12.35");
    const input = screen.getByLabelText("Other amount in dollars");

    await userEvent.type(input, "abc");
    expect(screen.getByText("Enter an amount in dollars, like 25.")).toBeInTheDocument();

    await userEvent.clear(input);
    await userEvent.type(input, "-5");
    expect(screen.getByText("Enter an amount in dollars, like 25.")).toBeInTheDocument();

    expect(
      client.calls.some(
        (call) =>
          call.path === "/api/v1/billing/topups" && (call.init.method ?? "GET") === "POST",
      ),
    ).toBe(false);
  });

  it("renders the low-balance notice in plain words and never the word micros", async () => {
    const client = makeStubClient(
      baseRoutes({ "/api/v1/billing/summary": makeSummary({ warning: "low" }) }),
    );
    renderWithProviders(<BalanceCard />, client);

    expect(await screen.findByText("Your credits are running low")).toBeInTheDocument();
    expect(
      screen.getByText("Add credits so your assistant keeps answering."),
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("micros");
  });

  it("renders the empty warning copy", async () => {
    const client = makeStubClient(
      baseRoutes({ "/api/v1/billing/summary": makeSummary({ warning: "empty" }) }),
    );
    renderWithProviders(<BalanceCard />, client);

    expect(await screen.findByText("You are out of credits")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Your assistant is not answering and campaigns are paused until you add credits.",
      ),
    ).toBeInTheDocument();
  });

  it("saves the auto-recharge settings as micros", async () => {
    const client = makeStubClient(
      baseRoutes({ "/api/v1/billing/summary": makeSummary({ auto_recharge: null }) }),
    );
    renderWithProviders(<BalanceCard />, client);

    await screen.findByText("$12.35");
    await userEvent.click(
      screen.getByLabelText("Top up automatically when my credits run low"),
    );

    const threshold = await screen.findByLabelText("Top up when my balance falls below");
    const amount = screen.getByLabelText("Amount to add each time");

    await userEvent.clear(threshold);
    await userEvent.type(threshold, "10");
    await userEvent.clear(amount);
    await userEvent.type(amount, "50");
    await userEvent.selectOptions(await screen.findByLabelText("Card to use"), "pm-1");

    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const patch = client.calls.find(
        (call) =>
          call.path === "/api/v1/billing/auto-recharge" && (call.init.method ?? "GET") === "PATCH",
      );
      expect(patch).toBeDefined();
      expect(patch?.init.json).toEqual({
        enabled: true,
        threshold_micros: 10_000_000,
        amount_micros: 50_000_000,
        payment_method_id: "pm-1",
      });
    });
  });

  it("keeps exactly what the customer typed in the threshold field", async () => {
    const client = makeStubClient(
      baseRoutes({ "/api/v1/billing/summary": makeSummary({ auto_recharge: null }) }),
    );
    renderWithProviders(<BalanceCard />, client);

    await screen.findByText("$12.35");
    await userEvent.click(
      screen.getByLabelText("Top up automatically when my credits run low"),
    );

    const threshold = await screen.findByLabelText("Top up when my balance falls below");
    await userEvent.clear(threshold);
    await userEvent.type(threshold, "5");

    expect(threshold).toHaveValue("5");
  });

  // Supervisor-added: the drafter hid the whole auto-recharge form (Save included) behind
  // the checkbox, which made automatic top-ups a one-way door - once on, there was no
  // control left to turn them off. This test is the fence around that fix.
  it("can turn automatic top-ups off again", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/summary": makeSummary({
          auto_recharge: {
            threshold_micros: 10_000_000,
            amount_micros: 50_000_000,
            payment_method_id: "pm-1",
          },
        }),
      }),
    );
    renderWithProviders(<BalanceCard />, client);

    const checkbox = await screen.findByLabelText(
      "Top up automatically when my credits run low",
    );
    expect(checkbox).toBeChecked();

    await userEvent.click(checkbox);
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const patch = client.calls.find(
        (call) =>
          call.path === "/api/v1/billing/auto-recharge" &&
          (call.init.method ?? "GET") === "PATCH",
      );
      expect(patch).toBeDefined();
      expect((patch?.init.json as { enabled: boolean }).enabled).toBe(false);
    });
  });

  it("disables all add-credits controls without org billing permission", async () => {
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/me/capabilities": {
          ...CAPABILITIES,
          permissions: ["settings:read"],
        },
      }),
    );
    renderWithProviders(<BalanceCard />, client);

    expect(await screen.findByText("$12.35")).toBeInTheDocument();
    expect(
      screen.getByText("Only the workspace owner can add credits."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "$25.00" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add credits" })).toBeDisabled();
  });

  it("shows a summary error with a retry that recovers", async () => {
    let summaryCalls = 0;
    const client = makeStubClient(
      baseRoutes({
        "/api/v1/billing/summary": ((_path, _init) => {
          summaryCalls += 1;
          if (summaryCalls === 1) return new Error("billing down");
          return makeSummary();
        }) as RouteStub,
      }),
    );
    renderWithProviders(<BalanceCard />, client);

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("billing down")).toBeInTheDocument();

    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("$12.35")).toBeInTheDocument();
  });
});
