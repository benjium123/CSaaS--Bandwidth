import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import { TenDlcRegistration } from "./TenDlcRegistration";
import { TollFreeVerificationCard } from "./TollFreeVerificationCard";
import type { Me } from "@/auth/AuthContext";
import { makeStubClient, renderWithProviders } from "@/test/harness";

const INDIVIDUAL_NOTICE = "Individual accounts support calling only. SMS and MMS are unavailable.";

/**
 * The AuthProvider fetches /auth/me itself. An individual membership is what routes both
 * wrappers to their notice; the registration and TFV endpoints are deliberately NOT stubbed
 * so that any request to them throws "No stub for ..." and fails the test loudly.
 */
function individualRoutes() {
  // hasPermission reads Me.permissions; the wrappers gate on membership.account_type, so an
  // empty permission list is fine here and keeps the fixture honest about what is exercised.
  const me: Me = {
    id: "u-1",
    email: "ada@example.com",
    full_name: "Ada Lovelace",
    permissions: [],
    memberships: [
      {
        org_id: "org-1",
        org_name: "Ada's Workspace",
        org_slug: "ada-workspace",
        role_name: "owner",
        account_type: "individual",
      },
    ],
  };

  return {
    "/api/v1/auth/me": me,
    "/api/v1/me/capabilities": {
      permissions: ["compliance:read", "compliance:manage"],
      org: {
        has_provider: true,
        has_number: true,
        member_count: 1,
        registration_state: "approved",
      },
    },
  };
}

function registrationCalls(client: ReturnType<typeof makeStubClient>) {
  return client.calls.filter((call) => call.path.startsWith("/api/v1/registration"));
}

describe("individual registration wrappers", () => {
  it("TenDlcRegistration shows the explanation and never calls registration endpoints", async () => {
    const client = makeStubClient(individualRoutes());
    renderWithProviders(<TenDlcRegistration />, client);

    expect(await screen.findByText(INDIVIDUAL_NOTICE)).toBeInTheDocument();
    // The business panels are not mounted, so their queries never fire.
    expect(screen.queryByRole("button", { name: "Add brand" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add campaign" })).toBeNull();

    await waitFor(() => expect(registrationCalls(client)).toHaveLength(0));
  });

  it("TollFreeVerificationCard shows the explanation and never calls registration endpoints", async () => {
    const client = makeStubClient(individualRoutes());
    renderWithProviders(<TollFreeVerificationCard />, client);

    expect(await screen.findByText(INDIVIDUAL_NOTICE)).toBeInTheDocument();
    expect(screen.queryByLabelText("Business name")).toBeNull();
    expect(screen.queryByRole("button", { name: "Create verification" })).toBeNull();

    await waitFor(() => expect(registrationCalls(client)).toHaveLength(0));
  });
});
