import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { PlanCard } from "./PlanCard";

const me = {
  id: "u1",
  email: "owner@example.com",
  full_name: "Owner",
  memberships: [{ org_id: "org-1", org_name: "Org", org_slug: "org", role_name: "owner", permissions: ["*"] }],
};

// Solo + 2 users + 2 numbers = $55: the case Team exists for.
const plan = {
  plan: { code: "solo", name: "Solo", status: "active", price_cents: 1500, monthly_total_cents: 5500, renews_at: null, cancel_at_period_end: false },
  users: { limit: 3, in_use: 3, included: 1, extra: 2 },
  numbers: { limit: 3, in_use: 3, included: 1, extra: 2 },
  minutes: { included: 600, remaining: 540 },
  extra_user_cents: 1500,
  extra_number_cents: 500,
  minutes_per_user: 200,
  catalog: [
    { code: "solo", name: "Solo", users: 1, numbers: 1, price_cents: 1500, minutes: 200, monthly_total_cents_if_switched: 5500 },
    { code: "team", name: "Team", users: 3, numbers: 3, price_cents: 4500, minutes: 600, monthly_total_cents_if_switched: 4500 },
  ],
};

describe("PlanCard", () => {
  it("shows the plan and usage, and switching sends the exact new total the owner confirmed", async () => {
    const client = makeStubClient({
      "/api/v1/auth/me": me,
      "/api/v1/billing/plan": plan,
      "/api/v1/billing/plan/change": { ...plan, plan: { ...plan.plan, code: "team", name: "Team", monthly_total_cents: 4500 } },
    });
    renderWithProviders(<PlanCard />, client);

    expect(await screen.findByText("Solo plan")).toBeInTheDocument();
    expect(screen.getByText("1 included + 2 × $15")).toBeInTheDocument();
    expect(screen.getByText("540 of 600 left")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Switch · $45/mo" }));
    expect(screen.getByRole("region", { name: "Confirm plan change" })).toHaveTextContent("$45/month");
    expect(client.calls.some(c => c.path === "/api/v1/billing/plan/change")).toBe(false);

    await userEvent.click(screen.getByRole("button", { name: "Switch to Team" }));
    await waitFor(() =>
      expect(client.calls.find(c => c.path === "/api/v1/billing/plan/change")?.init.json).toEqual({
        plan_code: "team",
        accept_cents: 4500,
      }),
    );
  });
});
