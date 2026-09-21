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
    expect(approve).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(screen.getAllByText(/I call my clients/)[0]).toHaveTextContent("In my own words.");
    await userEvent.click(approve);
    await waitFor(() => expect(client.calls.some(c => c.path.endsWith("/approve") && (c.init.json as { manual_override?: boolean })?.manual_override === true)).toBe(true));
    await userEvent.click(screen.getByRole("link", { name: "Review queue" }));
    expect(await screen.findByRole("button", { name: /Ada/ })).toBeInTheDocument();
  });

  it("shows $45 monthly for three selected phone numbers", async () => {
    const numbers = ["+12125550101", "+12125550102", "+12125550103"].map(e164 => ({ e164, locality: "New York", region: "NY" }));
    const client = makeStubClient({
      "/api/v1/auth/me": me,
      "/api/v1/billing/number-purchases/current": null,
      "/api/v1/numbers/available?carrier=telnyx&area_code=212&limit=20": numbers,
      "/api/v1/billing/number-checkout": { id: "purchase-1", state: "checkout", numbers: [] },
    });
    renderWithProviders(<ChooseNumbersPage />, client);
    await userEvent.type(screen.getByLabelText("Area code"), "212");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    for (const box of await screen.findAllByRole("checkbox")) await userEvent.click(box);
    expect(screen.getByText("3 numbers × $15/month")).toBeInTheDocument();
    expect(screen.getByText("$45")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Continue to payment" }));
    await waitFor(() => expect(client.calls.some(c => c.path === "/api/v1/billing/number-checkout" && (c.init.json as { numbers: string[] })?.numbers.length === 3)).toBe(true));
  });
});
