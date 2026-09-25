import { describe, it, expect } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { OpsPage } from "./OpsPage";
import { ChooseNumbersPage } from "./ChooseNumbersPage";

const me = { id: "u1", email: "admin@example.com", full_name: "Admin", permissions: ["numbers:manage"], memberships: [{ org_id: "org-1", role_name: "owner" }], is_platform_operator: true, operator_role: "admin" };

describe("Review and paid number setup", () => {
  it("lets super admins decide despite missing checks and shows original answers first", async () => {
    const application = { org: { id: "org-1", name: "Ada", slug: "ada" }, status: "submitted", account_type: "individual", business: { legal_name: "Ada", country: "US" }, use_case: { vertical: "Independent Artist", description: "I call my clients.\nIn my own words.", destination_countries: ["GB"] }, persons: [], documents: [], checks: {}, approval_blockers: ["Sanctions lists are not loaded"], risk: { tier: "low", reasons: [] }, limits: null, deposit_required_cents: null };
    const client = makeStubClient({
      "/api/v1/auth/me": me,
      "/api/v1/ops/queue": { applications: [{ org_id: "org-1", org_name: "Ada", legal_name: "Ada", status: "submitted", risk_tier: "low" }], open_security_alerts: 0 },
      "/api/v1/ops/applications/org-1": application,
      "/api/v1/ops/applications/org-1/approve": { ...application, status: "approved" },
    });
    renderWithProviders(<OpsPage />, client);
    await userEvent.click(await screen.findByRole("button", { name: /Ada/ }));
    expect(screen.getByRole("navigation", { name: "Administration navigation" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Billing" })).toHaveAttribute("href", "/?section=billing");
    const approve = await screen.findByRole("button", { name: "Approve" });
    // A failed check is never overridden by default: the admin ticks the override and
    // writes the reason first.
    expect(approve).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(screen.getAllByText(/I call my clients/)[0]).toHaveTextContent("In my own words.");
    await userEvent.click(screen.getByRole("checkbox", { name: /approve anyway/ }));
    expect(approve).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reviewer note"), "Sanctions list checked by hand");
    expect(approve).toBeEnabled();
    await userEvent.click(approve);
    await waitFor(() => expect(client.calls.some(c => c.path.endsWith("/approve") && (c.init.json as { manual_override?: boolean })?.manual_override === true)).toBe(true));
    await userEvent.click(screen.getByRole("link", { name: "Review queue" }));
    expect(await screen.findByRole("button", { name: /Ada/ })).toBeInTheDocument();
  });

  it("three numbers on Team are $45 a month, all included, and the plan is sent with the checkout", async () => {
    const numbers = ["+12125550101", "+12125550102", "+12125550103"].map(e164 => ({ e164, locality: "New York", region: "NY" }));
    const client = makeStubClient({
      "/api/v1/auth/me": me,
      "/api/v1/billing/number-purchases/current": null,
      "/api/v1/numbers/available?carrier=telnyx&area_code=212&limit=20": numbers,
      "/api/v1/numbers/emergency-addresses": { addresses: [], notice: "911 notice text" },
      "/api/v1/billing/number-checkout": { id: "purchase-1", state: "checkout", numbers: [] },
      "/api/v1/billing/plan": {
        plan: null, users: { limit: null, in_use: 1 }, numbers: { limit: null, in_use: 0 },
        extra_user_cents: 1500, extra_number_cents: 500, minutes_per_user: 200,
        catalog: [
          { code: "solo", name: "Solo", users: 1, numbers: 1, price_cents: 1500, minutes: 200, monthly_total_cents_if_switched: 1500 },
          { code: "team", name: "Team", users: 3, numbers: 3, price_cents: 4500, minutes: 600, monthly_total_cents_if_switched: 4500 },
        ],
      },
    });
    renderWithProviders(<ChooseNumbersPage />, client);
    await userEvent.type(screen.getByLabelText("Area code"), "212");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    for (const box of await screen.findAllByRole("checkbox", { name: /\+1212555010/ })) await userEvent.click(box);
    // On Solo the 2nd and 3rd numbers are $5 add-ons: $25. Team includes all three: $45.
    expect(screen.getByText("Includes 2 extra numbers at $5")).toBeInTheDocument();
    expect(screen.getByText("$25")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: /Team/ }));
    expect(screen.getByText("Monthly total")).toBeInTheDocument();
    expect(screen.getByText("Team: 3 users + 3 numbers")).toBeInTheDocument();
    expect(screen.getAllByText("$45").length).toBeGreaterThan(0);
    await userEvent.type(await screen.findByLabelText("Business or person at this location"), "Ada Studio");
    await userEvent.type(screen.getByLabelText("Street address"), "1 Main St");
    await userEvent.type(screen.getByLabelText("City"), "New York");
    await userEvent.type(screen.getByLabelText("State (2-letter)"), "NY");
    await userEvent.type(screen.getByLabelText("ZIP code"), "10001");
    await userEvent.click(screen.getByRole("checkbox", { name: "I understand how 911 works with these numbers" }));
    await userEvent.click(screen.getByRole("button", { name: "Continue to payment" }));
    await waitFor(() => expect(client.calls.some(c => c.path === "/api/v1/billing/number-checkout" && (c.init.json as { numbers: string[]; acknowledge_e911?: boolean })?.numbers.length === 3 && (c.init.json as { acknowledge_e911?: boolean })?.acknowledge_e911 === true && (c.init.json as { plan_code?: string })?.plan_code === "team")).toBe(true));
  });
});
