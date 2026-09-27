import { afterEach, describe, it, expect, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { AddCreditPage } from "./AddCreditPage";

const me = { id: "u1", email: "owner@example.com", full_name: "Owner", permissions: ["org:billing"], memberships: [{ org_id: "org-1", role_name: "owner" }] };

afterEach(() => vi.unstubAllGlobals());

describe("AddCreditPage (signup funding step)", () => {
  it("pays the chosen amount through Stripe and returns to onboarding", async () => {
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, search: "", assign });
    const client = makeStubClient({
      "/api/v1/auth/me": me,
      "/api/v1/billing/topups": { checkout_url: "https://checkout.stripe.test/cs_credit" },
    });
    renderWithProviders(<AddCreditPage />, client);

    await userEvent.click(screen.getByRole("radio", { name: "$25" }));
    await userEvent.click(screen.getByRole("button", { name: "Pay $25.00 with Stripe" }));

    await waitFor(() => expect(assign).toHaveBeenCalledWith("https://checkout.stripe.test/cs_credit"));
    expect(client.calls.find(c => c.path === "/api/v1/billing/topups")?.init.json).toEqual({
      amount_micros: 25_000_000,
      return_to: "onboarding",
    });
    expect(screen.getByText(/Auto-recharge is switched on/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Refund policy" })).toHaveAttribute("href", "/legal/refunds");
  });

  it("refuses less than $5", async () => {
    const client = makeStubClient({ "/api/v1/auth/me": me });
    renderWithProviders(<AddCreditPage />, client);

    const other = screen.getByLabelText("Other amount in dollars");
    await userEvent.clear(other);
    await userEvent.type(other, "4");
    expect(screen.getByRole("alert")).toHaveTextContent("between $5 and $5,000");
    expect(screen.getByRole("button", { name: "Pay with Stripe" })).toBeDisabled();
  });
});
