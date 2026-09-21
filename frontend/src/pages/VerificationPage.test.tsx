import { describe, it, expect } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { VerificationPage } from "./VerificationPage";
import { makeStubClient, renderWithProviders } from "@/test/harness";
import { COUNTRIES } from "@/lib/countries";

function fixture(verified = true) {
  return {
    account_type: "individual", status: "draft",
    business: { legal_name: "Ada Solo", country: "PK", business_phone: "+923001234567" },
    use_case: { vertical: "Consulting", business_description: "Business consulting", description: "Calling customers", destination_countries: ["NZ"] },
    persons: [{ id: "p1", role: "owner", is_you: true, status: verified ? "verified" : "not_started", verified_at: "2026-09-21" }],
    agreement: { current_version: "v1", accepted_version: null, accepted_at: null }, missing: ["agreement"],
  };
}
function clientFor(profile = fixture()) {
  return makeStubClient({
    "/api/v1/auth/me": { id: "u1", email: "ada@example.com", full_name: "Ada Solo", permissions: ["org:update"], memberships: [{ org_id: "org-1", permissions: ["org:update"], role_name: "owner" }] },
    "/api/v1/kyc/profile": profile,
    "/api/v1/kyc/application": profile,
    "/api/v1/kyc/agreement": {},
    "/api/v1/kyc/submit": {},
  });
}
describe("Standalone personal verification", () => {
  it("offers global countries and prefixes with only the requested personal fields", async () => {
    renderWithProviders(<VerificationPage />, clientFor());
    expect(await screen.findByLabelText("Legal name")).toHaveValue("Ada Solo");
    expect(screen.getByLabelText("Country")).toHaveValue("PK");
    expect(screen.getByLabelText("Phone country prefix")).toHaveValue("PK");
    expect(screen.getByLabelText("Phone number")).toHaveValue("3001234567");
    expect(screen.getByLabelText("Customer country")).toHaveValue("NZ");
    expect(COUNTRIES.length).toBeGreaterThanOrEqual(249);
    expect(COUNTRIES.some(c => c.value === "PN")).toBe(true);
    expect(screen.queryByLabelText("Calls per month")).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });
  it("requires agreement and sends the short form before submitting", async () => {
    const client = clientFor();
    renderWithProviders(<VerificationPage />, client);
    const submit = await screen.findByRole("button", { name: "Submit for review" });
    expect(submit).toBeDisabled();
    await userEvent.click(screen.getByRole("checkbox", { name: "I accept the agreement" }));
    await userEvent.clear(screen.getByLabelText("Describe your business"));
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Describe your business"), "I advise businesses");
    await userEvent.click(submit);
    await waitFor(() => expect(client.calls.some(c => c.path === "/api/v1/kyc/submit")).toBe(true));
    const saved = client.calls.find(c => c.path === "/api/v1/kyc/application");
    expect(saved?.init.json).toMatchObject({ legal_name: "Ada Solo", country: "PK", phone: "+923001234567", industry: "Consulting", business_description: "I advise businesses", customer_country: "NZ" });
    expect(client.calls.findIndex(c => c.path === "/api/v1/kyc/agreement")).toBeLessThan(client.calls.findIndex(c => c.path === "/api/v1/kyc/submit"));
  });
  it("does not allow an unverified identity to submit", async () => {
    renderWithProviders(<VerificationPage />, clientFor(fixture(false)));
    await screen.findByRole("button", { name: "Verify with Didit" });
    await userEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("button", { name: "Submit for review" })).toBeDisabled();
  });
});
