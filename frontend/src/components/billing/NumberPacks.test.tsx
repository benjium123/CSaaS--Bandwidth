import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/api/client";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { PlanCard } from "./PlanCard";

const me = {
  id: "u1",
  email: "owner@example.com",
  full_name: "Owner",
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner", permissions: ["*"] }],
};

// $95/$175/$300 for 25/50/100 numbers: cheaper per number than the $5 add-on.
const pack25 = { code: "25", size: 25, list_price_cents: 9500, price_cents: 9500, per_number_cents: 380, owned: 0, available: true };
const pack50 = { code: "50", size: 50, list_price_cents: 17500, price_cents: 17500, per_number_cents: 350, owned: 0, available: true };
const pack100 = { code: "100", size: 100, list_price_cents: 30000, price_cents: 30000, per_number_cents: 300, owned: 0, available: true };

// Solo + 2 users + 2 numbers = $55: the case Team exists for.
const plan = {
  plan: { code: "solo", name: "Solo", status: "active", price_cents: 1500, monthly_total_cents: 5500, renews_at: null, cancel_at_period_end: false },
  users: { limit: 3, in_use: 3, included: 1, extra: 2 },
  numbers: { limit: 3, in_use: 3, included: 1, extra: 2, in_packs: 0 },
  minutes: { included: 600, remaining: 540 },
  extra_user_cents: 1500,
  extra_number_cents: 500,
  minutes_per_user: 200,
  number_packs: [pack25, pack50, pack100],
  catalog: [
    { code: "solo", name: "Solo", users: 1, numbers: 1, price_cents: 1500, minutes: 200, monthly_total_cents_if_switched: 5500 },
    { code: "team", name: "Team", users: 3, numbers: 3, price_cents: 4500, minutes: 600, monthly_total_cents_if_switched: 4500 },
  ],
};

const quoteError = (cents: number) =>
  new ApiError(409, "price_confirmation_required", "Confirm the new price to continue.", {
    code: "price_confirmation_required",
    quote: { monthly_increase_cents: cents, interval: "month" },
  });

describe("NumberPacks", () => {
  it("lists packs and points out when add-on numbers cost more than a pack", async () => {
    const manyNumbers = { ...plan, numbers: { ...plan.numbers, limit: 21, in_use: 21, extra: 20 } };
    const client = makeStubClient({
      "/api/v1/auth/me": me,
      "/api/v1/billing/plan": manyNumbers,
    });
    renderWithProviders(<PlanCard />, client);

    expect(await screen.findByText("Number packs")).toBeInTheDocument();
    expect(screen.getByText("25 numbers")).toBeInTheDocument();
    expect(screen.getByText("$95/mo")).toBeInTheDocument();
    expect(screen.getByText("$3.80 each")).toBeInTheDocument();
    expect(
      screen.getByText("You pay $100/mo for 20 add-on numbers. A 25-number pack is $95/mo."),
    ).toBeInTheDocument();
  });

  it("asks for confirmation and sends the quoted amount back when adding", async () => {
    let attempt = 0;
    const client = makeStubClient({
      "/api/v1/auth/me": me,
      "/api/v1/billing/plan": plan,
      "/api/v1/billing/plan/packs": () => {
        attempt += 1;
        if (attempt === 1) throw quoteError(-2000);
        return plan;
      },
    });
    const posts = () => client.calls.filter(c => c.path === "/api/v1/billing/plan/packs");
    renderWithProviders(<PlanCard />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Add 25-number pack" }));
    await waitFor(() => expect(posts()[0]?.init.json).toEqual({ code: "25", accept_cents: null }));

    expect(await screen.findByRole("region", { name: "Confirm number pack" })).toHaveTextContent("drops by $20");
    expect(posts()).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "Add pack" }));
    await waitFor(() => expect(posts()[1]?.init.json).toEqual({ code: "25", accept_cents: -2000 }));
  });

  it("shows a positive quote as money added per month", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": me,
      "/api/v1/billing/plan": plan,
      "/api/v1/billing/plan/packs": () => {
        throw quoteError(10500);
      },
    });
    renderWithProviders(<PlanCard />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Add 25-number pack" }));
    expect(await screen.findByRole("region", { name: "Confirm number pack" })).toHaveTextContent("$105 more per month");
  });

  it("does not remove a pack until it is confirmed", async () => {
    const owned = { ...plan, number_packs: [{ ...pack25, owned: 1 }] };
    const client = makeStubClient({
      "/api/v1/auth/me": me,
      "/api/v1/billing/plan": owned,
      "/api/v1/billing/plan/packs/25": owned,
    });
    renderWithProviders(<PlanCard />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Remove 25-number pack" }));
    expect(screen.getByRole("region", { name: "Confirm number pack" })).toHaveTextContent("No refund for this month.");
    expect(client.calls.some(c => c.init.method === "DELETE")).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "Remove pack" }));
    await waitFor(() => {
      const del = client.calls.find(c => c.init.method === "DELETE");
      expect(del?.path).toBe("/api/v1/billing/plan/packs/25");
    });
  });

  it("renders nothing when no pack can be bought or is owned", async () => {
    const hidden = { ...plan, number_packs: [{ ...pack25, available: false, owned: 0 }] };
    const client = makeStubClient({
      "/api/v1/auth/me": me,
      "/api/v1/billing/plan": hidden,
    });
    renderWithProviders(<PlanCard />, client);

    expect(await screen.findByText("Solo plan")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Number packs" })).not.toBeInTheDocument();
  });

  it("shows the server's explanation when a pack is still in use", async () => {
    const owned = { ...plan, number_packs: [{ ...pack25, owned: 1 }] };
    const client = makeStubClient({
      "/api/v1/auth/me": me,
      "/api/v1/billing/plan": owned,
      "/api/v1/billing/plan/packs/25": () => {
        throw new ApiError(409, "pack_in_use", "Move the numbers out of the pack before removing it.");
      },
    });
    renderWithProviders(<PlanCard />, client);

    await userEvent.click(await screen.findByRole("button", { name: "Remove 25-number pack" }));
    await userEvent.click(screen.getByRole("button", { name: "Remove pack" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Move the numbers out of the pack before removing it.");
  });
});
